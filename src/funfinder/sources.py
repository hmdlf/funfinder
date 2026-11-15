"""Список источников, площадок и мест публикации: файлы sources/<город>.yaml и
sources/venues/<город>.yaml → таблицы meta.cities, meta.sources, meta.venues,
meta.publish_targets.

Источники — по разделам платформ: vk: (сообщества VK), tg: (публичные
каналы Telegram) и sites: (сайты площадок с разбором афиши в funfinder/sites/).
Раздел publish: — куда DAG publish отправляет подборку «Завтра в <городе>»:
каналы Telegram (chat:) и сообщества VK (group:; draft_to: — полуавтомат,
текст для VK присылает бот в Telegram).

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
from funfinder.publish.digest import DigestSettings
from funfinder.sites.ingest import SiteSettings, normalize_domain
from funfinder.tg.ingest import TGSettings, normalize_channel
from funfinder.vk.ingest import IngestSettings, normalize_handle

SLUG_RE = re.compile(r"^[a-z0-9-]+$")
SHORT_NAME_LIMIT = 30


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
    short: str | None = None  # для подборок в каналах: «Буксир»


@dataclass(frozen=True)
class PublishConfig:
    platform: str  # tg или vk
    handle: str  # tg: @канал или числовой id чата; vk: id или короткое имя сообщества
    note: str | None = None
    settings: dict = field(default_factory=dict)
    draft_chat: str | None = None  # vk: куда в Telegram присылать текст для ручной публикации


@dataclass(frozen=True)
class CityConfig:
    slug: str
    name: str
    timezone: str
    language: str = "ru"
    vk: tuple[SourceConfig, ...] = ()
    tg: tuple[SourceConfig, ...] = ()
    site: tuple[SourceConfig, ...] = ()
    venues: tuple[VenueConfig, ...] = ()
    publish: tuple[PublishConfig, ...] = ()

    def sources(self, platform: str) -> tuple[SourceConfig, ...]:
        return getattr(self, platform)


# Платформа → (раздел в yaml, как привести имя к одному виду, класс настроек загрузки).
PLATFORMS = {
    "vk": ("vk", normalize_handle, IngestSettings),
    "tg": ("tg", normalize_channel, TGSettings),
    "site": ("sites", normalize_domain, SiteSettings),
}


_TG_CHAT_RE = re.compile(r"^(@[A-Za-z][A-Za-z0-9_]{3,31}|-?\d+)$")


def normalize_tg_chat(raw: str) -> str:
    """'https://t.me/matingu_test' или 'matingu_test' → '@matingu_test'; числовой id — как есть."""
    chat = raw.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if chat.lower().startswith(prefix):
            chat = chat[len(prefix) :].strip("/")
    if not chat.startswith("@") and not chat.lstrip("-").isdigit():
        chat = f"@{chat}"
    if not _TG_CHAT_RE.match(chat):
        raise ValueError(f"не похоже на канал Telegram: {raw!r} (нужно @имя или числовой id)")
    return chat


_VK_NUMERIC_RE = re.compile(r"^(?:club|public|event)?(\d+)$")


def normalize_vk_group(raw: str) -> str:
    """Сообщество VK для публикации: 'https://vk.com/club123' → '123', 'vk.com/matingu' → 'matingu'.

    Числовые адреса (club…, public…, -123) приводятся к числу: так их однозначно
    понимает groups.getById. Короткое имя остаётся именем.
    """
    raw = raw.strip()
    handle = normalize_handle(raw.lstrip("-") if raw.lstrip("-").isdigit() else raw)
    numeric = _VK_NUMERIC_RE.match(handle)
    return numeric.group(1) if numeric else handle


# Где публикуем подборки: платформа → (ключ адреса в yaml, как привести адрес к одному виду).
PUBLISH_PLATFORMS = {"tg": ("chat", normalize_tg_chat), "vk": ("group", normalize_vk_group)}


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
        unknown = set(item) - {"slug", "name", "address", "aliases", "short"}
        if unknown:
            raise SourcesConfigError(f"{where}: {slug}: неизвестные поля {sorted(unknown)}")
        aliases = tuple(str(a) for a in (item.get("aliases") or []))
        short = str(item["short"]).strip() if item.get("short") else None
        if short and len(short) > SHORT_NAME_LIMIT:
            raise SourcesConfigError(
                f"{where}: {slug}: short длиннее {SHORT_NAME_LIMIT} символов — это уже не короткое имя"
            )

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
                short=short,
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

    by_platform = {
        platform: _load_sources(path, data.get(section) or [], section, platform, venue_slugs)
        for platform, (section, _, _) in PLATFORMS.items()
    }
    return CityConfig(
        slug=slug,
        name=city["name"],
        timezone=tz,
        language=city.get("language", "ru"),
        venues=venues,
        publish=_load_publish(path, data.get("publish") or []),
        **by_platform,
    )


def _load_publish(path: Path, items: list) -> tuple[PublishConfig, ...]:
    result: list[PublishConfig] = []
    for i, item in enumerate(items):
        where = f"{path.name}: publish[{i}]"
        if not isinstance(item, dict) or not item.get("platform"):
            raise SourcesConfigError(f"{where} — нужен platform: tg или vk")
        platform = item["platform"]
        if platform not in PUBLISH_PLATFORMS:
            raise SourcesConfigError(
                f"{where}: публиковать в {platform!r} не умеем, можно: {', '.join(PUBLISH_PLATFORMS)}"
            )
        key, normalize = PUBLISH_PLATFORMS[platform]
        if not item.get(key):
            raise SourcesConfigError(f"{where} — для {platform} нужен {key}: куда публиковать")
        draft = item.get("draft_to")
        if draft is not None and platform != "vk":
            raise SourcesConfigError(f"{where}: draft_to — только для vk (текст для ручной публикации в VK)")
        try:
            handle = normalize(str(item[key]))
            draft_chat = normalize_tg_chat(str(draft)) if draft is not None else None
            DigestSettings.from_overrides(item.get("settings"))  # проверяем ключи заранее
        except ValueError as exc:
            raise SourcesConfigError(f"{where}: {exc}") from exc
        if any(t.platform == platform and t.handle == handle for t in result):
            raise SourcesConfigError(f"{path.name}: место публикации {handle} указано дважды")
        result.append(PublishConfig(platform, handle, item.get("note"), item.get("settings") or {}, draft_chat))
    return tuple(result)


def _load_sources(
    path: Path, items: list, section: str, platform: str, venue_slugs: set[str]
) -> tuple[SourceConfig, ...]:
    _, normalize, settings_cls = PLATFORMS[platform]
    result: list[SourceConfig] = []
    for i, item in enumerate(items):
        where = f"{path.name}: {section}[{i}]"
        if not isinstance(item, dict) or not item.get("handle"):
            raise SourcesConfigError(f"{where} — нужен как минимум handle")
        try:
            handle = normalize(str(item["handle"]))
        except ValueError as exc:
            raise SourcesConfigError(f"{where}: {exc}") from exc
        if any(s.handle == handle for s in result):
            raise SourcesConfigError(f"{path.name}: источник {handle} указан дважды")
        settings = item.get("settings") or {}
        try:
            settings_cls.from_overrides(settings)  # проверяем ключи заранее
        except ValueError as exc:
            raise SourcesConfigError(f"{where} ({handle}): {exc}") from exc
        venue = item.get("venue")
        if venue is not None and venue not in venue_slugs:
            raise SourcesConfigError(f"{where} ({handle}): площадки {venue!r} нет в venues/")
        result.append(SourceConfig(handle=handle, note=item.get("note"), settings=settings, venue=venue))
    return tuple(result)


def load_all(directory: Path | None = None) -> list[CityConfig]:
    directory = directory or sources_dir()
    configs = [load_city_config(p) for p in sorted(directory.glob("*.yaml"))]
    slugs = [c.slug for c in configs]
    duplicates = {s for s in slugs if slugs.count(s) > 1}
    if duplicates:
        raise SourcesConfigError(f"Город описан в нескольких файлах: {sorted(duplicates)}")
    return configs


def sync_city(conn, cfg: CityConfig) -> dict:
    """Записывает город, площадки и источники всех платформ в базу. Идемпотентно."""
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

        deactivated = 0
        for platform in PLATFORMS:
            for src in cfg.sources(platform):
                cur.execute(
                    """
                    INSERT INTO meta.sources (city_id, platform, handle, note, settings, venue_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (platform, handle) DO UPDATE
                       SET city_id = EXCLUDED.city_id,
                           note = EXCLUDED.note,
                           settings = EXCLUDED.settings,
                           venue_id = EXCLUDED.venue_id,
                           is_active = true,
                           updated_at = now()
                    """,
                    (city_id, platform, src.handle, src.note, Json(src.settings), venue_ids.get(src.venue)),
                )
            cur.execute(
                """
                UPDATE meta.sources
                   SET is_active = false, updated_at = now()
                 WHERE city_id = %s AND platform = %s AND is_active
                   AND NOT (handle = ANY(%s))
                """,
                (city_id, platform, [s.handle for s in cfg.sources(platform)]),
            )
            deactivated += cur.rowcount

        for target in cfg.publish:
            cur.execute(
                """
                INSERT INTO meta.publish_targets (city_id, platform, handle, note, settings, draft_chat)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (platform, handle) DO UPDATE
                   SET city_id = EXCLUDED.city_id,
                       note = EXCLUDED.note,
                       settings = EXCLUDED.settings,
                       draft_chat = EXCLUDED.draft_chat,
                       is_active = true,
                       updated_at = now()
                """,
                (city_id, target.platform, target.handle, target.note, Json(target.settings), target.draft_chat),
            )
        cur.execute(
            """
            UPDATE meta.publish_targets
               SET is_active = false, updated_at = now()
             WHERE city_id = %s AND is_active
               AND NOT ((platform || ' ' || handle) = ANY(%s))
            """,
            (city_id, [f"{t.platform} {t.handle}" for t in cfg.publish]),
        )

    return {
        "city": cfg.slug,
        "vk_sources": len(cfg.vk),
        "tg_sources": len(cfg.tg),
        "site_sources": len(cfg.site),
        "deactivated": deactivated,
        "venues": len(cfg.venues),
        "publish_targets": len(cfg.publish),
    }


def _sync_venues(cur, city_id: int, venues: tuple[VenueConfig, ...]) -> dict[str, int]:
    """Справочник площадок города → meta.venues и meta.venue_aliases. Возвращает slug → venue_id."""
    venue_ids: dict[str, int] = {}
    for v in venues:
        cur.execute(
            """
            INSERT INTO meta.venues (city_id, slug, name, address, short_name)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (city_id, slug) DO UPDATE
               SET name = EXCLUDED.name,
                   address = EXCLUDED.address,
                   short_name = EXCLUDED.short_name,
                   updated_at = now()
            RETURNING venue_id
            """,
            (city_id, v.slug, v.name, v.address, v.short),
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
