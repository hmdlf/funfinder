"""extract_events: новые и изменённые посты из raw.vk_posts → LLM → staging.events.

Какие посты берём: тех нет в журнале staging.vk_post_extractions, или у них
поменялся content_hash (организатор отредактировал пост). Старше
MAX_POST_AGE_DAYS не берём: их события почти наверняка прошли.

reextract=True дополнительно берёт посты, обработанные старой версией промпта
(PROMPT_VERSION). Стоит денег, поэтому только по явному запросу. Если задача
упадёт посередине, ретрай не тронет то, что уже перепрогнано новой версией.

Одна транзакция на пост: если задача упадёт посреди прогона, обработанные
посты останутся сохранёнными, а ретрай продолжит с того же места.
"""

import logging
import re
from collections import Counter
from datetime import UTC, date, datetime, time

from funfinder.events.llm import (
    PROMPT_VERSION,
    STATUSES,
    LLMResult,
    build_prompt,
    build_user_message,
)

log = logging.getLogger(__name__)

MAX_POST_AGE_DAYS = 30

SELECT_POSTS = """
SELECT p.owner_id, p.post_id, p.source_id, s.city_id, c.name, c.timezone,
       p.url, p.published_at, p.text, p.repost_text, p.content_hash
  FROM raw.vk_posts p
  JOIN meta.sources s ON s.source_id = p.source_id
  JOIN meta.cities c ON c.city_id = s.city_id
  LEFT JOIN staging.vk_post_extractions x ON x.owner_id = p.owner_id AND x.post_id = p.post_id
 WHERE p.published_at >= %(now)s - make_interval(days => %(max_age_days)s)
   AND (x.content_hash IS DISTINCT FROM p.content_hash
        OR x.status = 'timeout'  -- модель не успела ответить в прошлый раз
        OR (%(reextract)s AND x.prompt_version <> %(prompt_version)s))
 ORDER BY p.published_at
"""

INSERT_EVENT = """
INSERT INTO staging.events
       (source_id, city_id, owner_id, post_id, source_url, published_at,
        title, event_date, event_time, venue, price, category, status)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

UPSERT_JOURNAL = """
INSERT INTO staging.vk_post_extractions
       (owner_id, post_id, content_hash, status, model, prompt_version, tokens_in, tokens_out, raw_answer)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (owner_id, post_id) DO UPDATE SET
       content_hash   = EXCLUDED.content_hash,
       status         = EXCLUDED.status,
       model          = EXCLUDED.model,
       prompt_version = EXCLUDED.prompt_version,
       tokens_in      = EXCLUDED.tokens_in,
       tokens_out     = EXCLUDED.tokens_out,
       raw_answer     = EXCLUDED.raw_answer,
       processed_at   = now()
"""

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


# --- Очистка полей. Модель иногда пишет "null" строкой, "2026-11-25Tnull", "19:00-20:15". ---


def clean_text(value, limit: int = 500) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() in ("", "null", "none"):
        return None
    return s[:limit]


def clean_date(value) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def clean_time(value) -> time | None:
    if not isinstance(value, str):
        return None
    m = _TIME_RE.search(value)
    if not m:
        return None
    hour, minute = int(m[1]), int(m[2])
    return time(hour, minute) if hour < 24 and minute < 60 else None


def clean_status(value) -> str:
    s = (clean_text(value) or "").lower()
    return s if s in STATUSES else "scheduled"


def clean_category(value) -> str | None:
    s = clean_text(value, limit=50)
    return s.lower() if s else None


def post_text(text: str, repost_text: str) -> str:
    """Текст для модели: сам пост и текст репоста — анонс часто именно в репосте."""
    if repost_text.strip():
        return f"{text}\n\n[Репост]\n{repost_text}".strip()
    return text.strip()


def load_categories(conn) -> list[tuple[str, str]]:
    with conn, conn.cursor() as cur:
        cur.execute("SELECT slug, name_ru FROM meta.categories ORDER BY sort_order, slug")
        return cur.fetchall()


def save_result(conn, post: dict, result: LLMResult) -> None:
    """Старые упоминания поста → удалить, новые → вставить, журнал → обновить. Всё в одной транзакции."""
    with conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM staging.events WHERE owner_id = %s AND post_id = %s",
            (post["owner_id"], post["post_id"]),
        )
        for e in result.events:
            cur.execute(
                INSERT_EVENT,
                (
                    post["source_id"],
                    post["city_id"],
                    post["owner_id"],
                    post["post_id"],
                    post["url"],
                    post["published_at"],
                    clean_text(e.get("title")),
                    clean_date(e.get("date")),
                    clean_time(e.get("time")),
                    clean_text(e.get("venue")),
                    clean_text(e.get("price")),
                    clean_category(e.get("category")),
                    clean_status(e.get("status")),
                ),
            )
        cur.execute(
            UPSERT_JOURNAL,
            (
                post["owner_id"],
                post["post_id"],
                post["content_hash"],
                result.status,
                result.model,
                PROMPT_VERSION,
                result.tokens_in,
                result.tokens_out,
                result.raw_answer,
            ),
        )


def run(
    conn,
    llm,
    *,
    reextract: bool = False,
    max_age_days: int = MAX_POST_AGE_DAYS,
    now: datetime | None = None,
) -> dict:
    """llm — объект с методом extract(system_prompt, user_message) -> LLMResult."""
    system_prompt = build_prompt(load_categories(conn))
    params = {
        "now": now or datetime.now(UTC),
        "max_age_days": max_age_days,
        "reextract": reextract,
        "prompt_version": PROMPT_VERSION,
    }
    with conn, conn.cursor() as cur:
        cur.execute(SELECT_POSTS, params)
        columns = [d.name for d in cur.description]
        posts = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    log.info("Постов к обработке: %d", len(posts))

    stats = Counter()
    for n, post in enumerate(posts, start=1):
        if n % 50 == 0:
            log.info("Обработано %d из %d", n, len(posts))
        text = post_text(post.pop("text"), post.pop("repost_text"))
        if text:
            message = build_user_message(
                text, city=post["name"], published_at=post["published_at"], timezone=post["timezone"]
            )
            result = llm.extract(system_prompt, message)
        else:
            result = LLMResult(status="skipped_empty")
        save_result(conn, post, result)

        stats[result.status] += 1
        stats["events"] += len(result.events)
        stats["tokens_in"] += result.tokens_in
        stats["tokens_out"] += result.tokens_out
        if result.status in ("refused", "invalid_json", "timeout"):
            log.warning("%s: %s", post["url"], result.status)

    stats["posts"] = len(posts)
    return dict(stats)
