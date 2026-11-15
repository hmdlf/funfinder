"""Афиша сайта площадки → raw.site_events, по строке на показ.

Новые и изменённые показы (название, дата, время, ссылка, категория) записываются,
неизменённые только отмечаются как увиденные. Показ, который был в афише и
пропал, хотя его месяц на странице по-прежнему есть, помечается removed_at:
его отменили или перенесли. Если показ вернётся, отметка снимается.
"""

import hashlib
import logging
import re
from dataclasses import asdict, dataclass, fields
from datetime import date

from psycopg2.extras import execute_values

from funfinder.sites import PARSERS
from funfinder.sites.base import Listing

log = logging.getLogger(__name__)

DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


@dataclass(frozen=True)
class SiteSettings:
    category: str = "other"  # код из meta.categories: у театров — theatre

    @classmethod
    def from_overrides(cls, overrides: dict | None) -> "SiteSettings":
        overrides = overrides or {}
        known = {f.name for f in fields(cls)}
        unknown = set(overrides) - known
        if unknown:
            raise ValueError(f"неизвестные настройки {sorted(unknown)}, допустимы {sorted(known)}")
        return cls(**overrides)


@dataclass
class SiteIngestResult:
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    removed: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_domain(raw: str) -> str:
    """https://www.teatr-tolstogo.ru/afisha/time/ → teatr-tolstogo.ru. Разбор для домена должен быть в PARSERS."""
    domain = re.sub(r"^(https?://)?(www\.)?", "", raw.strip().lower()).split("/")[0]
    if not DOMAIN_RE.match(domain):
        raise ValueError(f"{raw!r} — не похоже на адрес сайта")
    if domain not in PARSERS:
        raise ValueError(f"для сайта {domain} нет разбора: добавьте модуль в funfinder/sites/ и строку в PARSERS")
    return domain


def content_hash(title: str, day: date, at, url: str, category: str | None = None) -> str:
    # Без категории — прежняя формула: у театров хэши не меняются, лишнего переноса в staging нет.
    tail = f"\x00{category}" if category else ""
    return hashlib.sha256(f"{title}\x00{day}\x00{at}\x00{url}{tail}".encode()).hexdigest()


_UPSERT_SQL = """
INSERT INTO raw.site_events
       (source_id, event_key, title, event_date, event_time, url, category, content_hash, ingest_run_id)
VALUES %s
ON CONFLICT (source_id, event_key) DO UPDATE
   SET title = EXCLUDED.title, event_date = EXCLUDED.event_date, event_time = EXCLUDED.event_time,
       url = EXCLUDED.url, category = EXCLUDED.category,
       content_hash = EXCLUDED.content_hash, ingest_run_id = EXCLUDED.ingest_run_id,
       removed_at = NULL, changed_at = now(), last_seen_at = now()
 WHERE raw.site_events.content_hash IS DISTINCT FROM EXCLUDED.content_hash
    OR raw.site_events.removed_at IS NOT NULL
RETURNING (xmax = 0) AS inserted
"""


def save_listing(conn, source_id: int, listing: Listing, *, run_id: str, today: date) -> SiteIngestResult:
    result = SiteIngestResult(fetched=len(listing.events))
    keys = [e.key for e in listing.events]
    with conn, conn.cursor() as cur:
        if listing.events:
            rows = [
                (
                    source_id,
                    e.key,
                    e.title,
                    e.day,
                    e.at,
                    e.url,
                    e.category,
                    content_hash(e.title, e.day, e.at, e.url, e.category),
                    run_id,
                )
                for e in listing.events
            ]
            returned = execute_values(cur, _UPSERT_SQL, rows, fetch=True)
            result.inserted = sum(1 for (inserted,) in returned if inserted)
            result.updated = len(returned) - result.inserted
            cur.execute(
                "UPDATE raw.site_events SET last_seen_at = now() WHERE source_id = %s AND event_key = ANY(%s)",
                (source_id, keys),
            )
        if listing.months:
            # Пропал из месяца, который на странице есть целиком, — значит, сняли. Прошедшие не трогаем.
            cur.execute(
                """
                UPDATE raw.site_events
                   SET removed_at = now()
                 WHERE source_id = %s AND removed_at IS NULL AND event_date >= %s
                   AND (extract(year FROM event_date)::int, extract(month FROM event_date)::int)
                       IN (SELECT y, m FROM unnest(%s::int[], %s::int[]) AS t(y, m))
                   AND NOT (event_key = ANY(%s))
                """,
                (
                    source_id,
                    today,
                    [y for y, _ in listing.months],
                    [m for _, m in listing.months],
                    keys,
                ),
            )
            result.removed = cur.rowcount
    return result


def ingest_site(fetcher, conn, source: dict, *, run_id: str, today: date) -> SiteIngestResult:
    """fetcher — объект с методом get(url) -> bytes (SiteFetcher)."""
    collect = PARSERS[source["handle"]]
    listing = collect(fetcher.get, today)
    result = save_listing(conn, source["source_id"], listing, run_id=run_id, today=today)
    log.info(
        "%s: показов %d (месяцы %s), новых %d, изменённых %d, снятых %d",
        source["handle"],
        result.fetched,
        sorted(listing.months),
        result.inserted,
        result.updated,
        result.removed,
    )
    return result
