"""Посты публичного канала Telegram → raw.tg_posts.

Логика та же, что у VK (funfinder.vk.ingest): новые посты — всё, что новее
водяного знака (максимального id в базе), плюс перепроверка последних
recheck_days дней на правки. Первая загрузка — за initial_lookback_days.
Строка переписывается, только если поменялся текст (content_hash).

Страницы листаются от новых к старым (?before=<id>), пока не дойдём и до
водяного знака, и до границы перепроверки — но не больше max_pages.
"""

import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta

from psycopg2.extras import execute_values

from funfinder.tg.fetch import page_url
from funfinder.tg.page import Page, TGPost, parse_page

log = logging.getLogger(__name__)

# Имя публичного канала: 5–32 символа, латиница, цифры, подчёркивание.
CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


@dataclass(frozen=True)
class TGSettings:
    initial_lookback_days: int = 30
    recheck_days: int = 7
    max_pages: int = 10  # по ~20 постов: на первую загрузку активного канала хватает
    page_delay_s: float = 1.0  # пауза между страницами: не нагружаем t.me

    @classmethod
    def from_overrides(cls, overrides: dict | None) -> "TGSettings":
        overrides = overrides or {}
        known = {f.name for f in fields(cls)}
        unknown = set(overrides) - known
        if unknown:
            raise ValueError(f"неизвестные настройки {sorted(unknown)}, допустимы {sorted(known)}")
        settings = cls(**overrides)
        for name in ("initial_lookback_days", "recheck_days", "max_pages"):
            if getattr(settings, name) < 1:
                raise ValueError(f"{name} должно быть положительным")
        if settings.page_delay_s < 0:
            raise ValueError("page_delay_s не может быть отрицательной")
        return settings


@dataclass
class TGIngestResult:
    pages: int = 0
    fetched: int = 0
    inserted: int = 0
    updated: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_channel(raw: str) -> str:
    """https://t.me/sinitsabar, t.me/s/sinitsabar, @sinitsabar → sinitsabar."""
    channel = raw.strip()
    channel = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/(s/)?", "", channel)
    channel = channel.lstrip("@").split("?")[0].strip("/")
    if not CHANNEL_RE.match(channel):
        raise ValueError(f"{raw!r} — не похоже на имя публичного канала Telegram")
    return channel


def content_hash(text: str, reply_text: str) -> str:
    return hashlib.sha256(f"{text}\x00{reply_text}".encode()).hexdigest()


def collect_posts(
    fetch_page: Callable[[int | None], Page],
    *,
    watermark: int | None,
    cutoff: datetime,
    settings: TGSettings,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[TGPost], int]:
    """Листает страницы от новых к старым. Возвращает (посты, число страниц)."""
    found: dict[int, TGPost] = {}
    before, pages = None, 0
    while pages < settings.max_pages:
        if pages:
            sleep(settings.page_delay_s)
        page = fetch_page(before)
        pages += 1
        for post in page.posts:
            found[post.post_id] = post
        if not page.posts or page.before is None:
            break  # старше постов нет
        oldest = min(page.posts, key=lambda p: p.post_id)
        reached_watermark = watermark is None or oldest.post_id <= watermark
        if reached_watermark and oldest.published_at < cutoff:
            break
        if before is not None and page.before >= before:
            log.warning("t.me вернул ту же страницу ещё раз (before=%s) — дальше не листаем", before)
            break
        before = page.before
    else:
        log.warning("Дошли до max_pages=%d, а новые посты ещё не кончились: часть пропущена", settings.max_pages)

    keep = [p for p in found.values() if p.published_at >= cutoff or (watermark is not None and p.post_id > watermark)]
    return sorted(keep, key=lambda p: p.post_id), pages


_UPSERT_SQL = """
INSERT INTO raw.tg_posts (
    source_id, post_id, channel, published_at, text, reply_text, media_count, url, content_hash, ingest_run_id
)
VALUES %s
ON CONFLICT (source_id, post_id) DO UPDATE
   SET channel       = EXCLUDED.channel,
       published_at  = EXCLUDED.published_at,
       text          = EXCLUDED.text,
       reply_text    = EXCLUDED.reply_text,
       media_count   = EXCLUDED.media_count,
       url           = EXCLUDED.url,
       content_hash  = EXCLUDED.content_hash,
       ingest_run_id = EXCLUDED.ingest_run_id,
       changed_at    = now()
 WHERE raw.tg_posts.content_hash IS DISTINCT FROM EXCLUDED.content_hash
RETURNING (xmax = 0) AS inserted
"""
# Как у VK: RETURNING отдаёт только вставленные и реально изменённые строки, xmax = 0 — вставленные.


def ingest_channel(
    fetcher,
    conn,
    source: dict,
    *,
    run_id: str,
    settings: TGSettings | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> TGIngestResult:
    """fetcher — объект с методом get(url) -> str (DirectFetcher или SSHFetcher)."""
    settings = settings or TGSettings.from_overrides(source.get("settings"))
    now = now or datetime.now(UTC)
    channel = source["handle"]

    with conn, conn.cursor() as cur:
        cur.execute("SELECT max(post_id) FROM raw.tg_posts WHERE source_id = %s", (source["source_id"],))
        watermark = cur.fetchone()[0]

    days = settings.initial_lookback_days if watermark is None else settings.recheck_days
    posts, pages = collect_posts(
        lambda before: parse_page(fetcher.get(page_url(channel, before)), channel),
        watermark=watermark,
        cutoff=now - timedelta(days=days),
        settings=settings,
        sleep=sleep,
    )
    result = TGIngestResult(pages=pages, fetched=len(posts))
    if not posts:
        return result

    rows = [
        (
            source["source_id"],
            p.post_id,
            channel,
            p.published_at,
            p.text,
            p.reply_text,
            p.media_count,
            p.url,
            content_hash(p.text, p.reply_text),
            run_id,
        )
        for p in posts
    ]
    with conn, conn.cursor() as cur:
        returned = execute_values(cur, _UPSERT_SQL, rows, fetch=True)
    result.inserted = sum(1 for (inserted,) in returned if inserted)
    result.updated = len(returned) - result.inserted
    log.info(
        "%s: страниц %d, получено %d, новых %d, изменённых %d (водяной знак %s)",
        channel,
        result.pages,
        result.fetched,
        result.inserted,
        result.updated,
        watermark,
    )
    return result
