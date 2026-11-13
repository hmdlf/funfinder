"""Список источников и площадок: файлы sources/<город>.yaml и
sources/venues/<город>.yaml → таблицы meta.cities, meta.sources, meta.venues.

Файл — источник правды: добавили паблик в yaml, на следующем запуске он
появится в базе. Убрали из yaml — источник станет неактивным (is_active =
false), а уже загруженные посты останутся. Площадки, убранные из
справочника, удаляются: привязанные к ним события просто теряют привязку.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from psycopg2.extras import Json

from funfinder.events.venues import is_generic, venue_tokens
from funfinder.vk.ingest import IngestSettings, normalize_handle

SLUG_RE = re.compile(r"^[a-z0-9-]+$")


class SourcesConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SourceConfig:
    handle: str
    note: str | None = None
    settings: dict = field(default_factory=dict)
    venue: str | None = None  # slug площадки по умолчанию: для пабликов самих площадок


@dataclass(frozen=True)
class VenueConfig:
    slug: str
    name: str
    address: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class CityConfig:
    slug: str
    name: str
    timezone: str
    language: str = "ru"
    vk: tuple[SourceConfig, ...] = ()
    venues: tuple[VenueConfig, ...] = ()


def sources_dir() -> Path:
    return Path(os.environ.get("FUNFINDER_SOURCES_DIR", "sources"))


def _read_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SourcesConfigError(f"{path.name}: не читается как YAML: {exc}") from exc


def load_venues(path: Path) -> tuple[VenueConfig, ...]:
    """Справочник площадок города. Файла нет — справочник пустой."""
    if not path.exists():
        return ()
    data = _read_yaml(path) or []
    where = f"venues/{path.name}"
    if not isinstance(data, list):
        raise SourcesConfigError(f"{where}: ожидается список площадок")

    venues: list[VenueConfig] = []
    owners: dict[frozenset, str] = {}  # основы варианта написания → slug площадки
    for i, item in enumerate(data):
        if not isinstance(item, dict) or not item.get("slug") or not item.get("name"):
            raise SourcesConfigError(f"{where}: [{i}] — нужны как минимум slug и name")
        slug = str(item["slug"])
        if not SLUG_RE.match(slug):
            raise SourcesConfigError(f"{where}: slug {slug!r} должен состоять из a-z, 0-9 и дефиса")
        if any(v.slug == slug for v in venues):
            raise SourcesConfigError(f"{where}: площадка {slug} указана дважды")
        unknown = set(item) - {"slug", "name", "address", "aliases"}
        if unknown:
            raise SourcesConfigError(f"{where}: {slug}: неизвестные поля {sorted(unknown)}")
        aliases = tuple(str(a) for a in (item.get("aliases") or []))

        for variant in (item["name"], *aliases):
            if is_generic(variant):
                # «Большой зал» подошёл бы к любому месту с большим залом.
                raise SourcesConfigError(f"{where}: {slug}: вариант {variant!r} не называет площадку")
            tokens = venue_tokens(variant)
            if owners.get(tokens, slug) != slug:
                raise SourcesConfigError(
                    f"{where}: вариант {variant!r} есть и у {owners[tokens]}, и у {slug} — непонятно, куда привязывать"
                )
            owners[tokens] = slug

        venues.append(
            VenueConfig(
                slug=slug,
                name=str(item["name"]),
                address=str(item["address"]) if item.get("address") else None,
                aliases=aliases,
            )
        )
    return tuple(venues)


def load_city_config(path: Path) -> CityConfig:
    data = _read_yaml(path) or {}

    city = data.get("city") or {}
    slug = city.get("slug")
    if not slug or not SLUG_RE.match(slug):
        raise SourcesConfigError(f"{path.name}: city.slug должен состоять из a-z, 0-9 и дефиса")
    if not city.get("name"):
        raise SourcesConfigError(f"{path.name}: не указан city.name")
    tz = city.get("timezone")
    try:
        ZoneInfo(tz or "")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SourcesConfigError(f"{path.name}: неизвестный часовой пояс {tz!r}") from exc

    venues = load_venues(path.parent / "venues" / f"{slug}.yaml")
    venue_slugs = {v.slug for v in venues}

    vk_sources: list[SourceConfig] = []
    seen: set[str] = set()
    for i, item in enumerate(data.get("vk") or []):
        if not isinstance(item, dict) or not item.get("handle"):
            raise SourcesConfigError(f"{path.name}: vk[{i}] — нужен как минимум handle")
        handle = normalize_handle(str(item["handle"]))
        if handle in seen:
            raise SourcesConfigError(f"{path.name}: источник {handle} указан дважды")
        seen.add(handle)
        settings = item.get("settings") or {}
        try:
            IngestSettings.from_overrides(settings)  # проверяем ключи заранее
        except ValueError as exc:
            raise SourcesConfigError(f"{path.name}: vk[{i}] ({handle}): {exc}") from exc
        venue = item.get("venue")
        if venue is not None and venue not in venue_slugs:
            raise SourcesConfigError(f"{path.name}: vk[{i}] ({handle}): площадки {venue!r} нет в venues/{slug}.yaml")
        vk_sources.append(SourceConfig(handle=handle, note=item.get("note"), settings=settings, venue=venue))

    return CityConfig(
        slug=slug,
        name=city["name"],
        timezone=tz,
        language=city.get("language", "ru"),
        vk=tuple(vk_sources),
        venues=venues,
    )


def load_all(directory: Path | None = None) -> list[CityConfig]:
    directory = directory or sources_dir()
    configs = [load_city_config(p) for p in sorted(directory.glob("*.yaml"))]
    slugs = [c.slug for c in configs]
    duplicates = {s for s in slugs if slugs.count(s) > 1}
    if duplicates:
        raise SourcesConfigError(f"Город описан в нескольких файлах: {sorted(duplicates)}")
    return configs


def sync_city(conn, cfg: CityConfig) -> dict:
    """Записывает город и его VK-источники в базу. Идемпотентно."""
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.cities (slug, name, timezone, language)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (slug) DO UPDATE
               SET name = EXCLUDED.name,
                   timezone = EXCLUDED.timezone,
                   language = EXCLUDED.language,
                   updated_at = now()
            RETURNING city_id
            """,
            (cfg.slug, cfg.name, cfg.timezone, cfg.language),
        )
        city_id = cur.fetchone()[0]
        venue_ids = _sync_venues(cur, city_id, cfg.venues)

        for src in cfg.vk:
            cur.execute(
                """
                INSERT INTO meta.sources (city_id, platform, handle, note, settings, venue_id)
                VALUES (%s, 'vk', %s, %s, %s, %s)
                ON CONFLICT (platform, handle) DO UPDATE
                   SET city_id = EXCLUDED.city_id,
                       note = EXCLUDED.note,
                       settings = EXCLUDED.settings,
                       venue_id = EXCLUDED.venue_id,
                       is_active = true,
                       updated_at = now()
                """,
                (city_id, src.handle, src.note, Json(src.settings), venue_ids.get(src.venue)),
            )

        cur.execute(
            """
            UPDATE meta.sources
               SET is_active = false, updated_at = now()
             WHERE city_id = %s AND platform = 'vk' AND is_active
               AND NOT (handle = ANY(%s))
            """,
            (city_id, [s.handle for s in cfg.vk]),
        )
        deactivated = cur.rowcount

    return {"city": cfg.slug, "vk_sources": len(cfg.vk), "deactivated": deactivated, "venues": len(cfg.venues)}


def _sync_venues(cur, city_id: int, venues: tuple[VenueConfig, ...]) -> dict[str, int]:
    """Справочник площадок города → meta.venues и meta.venue_aliases. Возвращает slug → venue_id."""
    venue_ids: dict[str, int] = {}
    for v in venues:
        cur.execute(
            """
            INSERT INTO meta.venues (city_id, slug, name, address)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (city_id, slug) DO UPDATE
               SET name = EXCLUDED.name,
                   address = EXCLUDED.address,
                   updated_at = now()
            RETURNING venue_id
            """,
            (city_id, v.slug, v.name, v.address),
        )
        venue_ids[v.slug] = cur.fetchone()[0]

    # Убранные из справочника — удаляем: ссылки на них в событиях обнулятся (ON DELETE SET NULL).
    cur.execute(
        "DELETE FROM meta.venues WHERE city_id = %s AND NOT (slug = ANY(%s))",
        (city_id, list(venue_ids)),
    )
    # Варианты написания проще переписать целиком, чем сверять по одному.
    cur.execute(
        "DELETE FROM meta.venue_aliases WHERE venue_id IN (SELECT venue_id FROM meta.venues WHERE city_id = %s)",
        (city_id,),
    )
    for v in venues:
        for alias in dict.fromkeys(v.aliases):  # без повторов, порядок как в файле
            cur.execute(
                "INSERT INTO meta.venue_aliases (venue_id, alias) VALUES (%s, %s)",
                (venue_ids[v.slug], alias),
            )
    return venue_ids
