"""Список источников: файлы sources/<город>.yaml → таблицы meta.cities и meta.sources.

Файл — источник правды: добавили паблик в yaml, на следующем запуске он
появится в базе. Убрали из yaml — источник станет неактивным (is_active =
false), а уже загруженные посты останутся.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from psycopg2.extras import Json

from funfinder.vk.ingest import IngestSettings, normalize_handle

SLUG_RE = re.compile(r"^[a-z0-9-]+$")


class SourcesConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SourceConfig:
    handle: str
    note: str | None = None
    settings: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CityConfig:
    slug: str
    name: str
    timezone: str
    language: str = "ru"
    vk: tuple[SourceConfig, ...] = ()


def sources_dir() -> Path:
    return Path(os.environ.get("FUNFINDER_SOURCES_DIR", "sources"))


def load_city_config(path: Path) -> CityConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SourcesConfigError(f"{path.name}: не читается как YAML: {exc}") from exc

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
        vk_sources.append(SourceConfig(handle=handle, note=item.get("note"), settings=settings))

    return CityConfig(
        slug=slug,
        name=city["name"],
        timezone=tz,
        language=city.get("language", "ru"),
        vk=tuple(vk_sources),
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

        for src in cfg.vk:
            cur.execute(
                """
                INSERT INTO meta.sources (city_id, platform, handle, note, settings)
                VALUES (%s, 'vk', %s, %s, %s)
                ON CONFLICT (platform, handle) DO UPDATE
                   SET city_id = EXCLUDED.city_id,
                       note = EXCLUDED.note,
                       settings = EXCLUDED.settings,
                       is_active = true,
                       updated_at = now()
                """,
                (city_id, src.handle, src.note, Json(src.settings)),
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

    return {"city": cfg.slug, "vk_sources": len(cfg.vk), "deactivated": deactivated}
