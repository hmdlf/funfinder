"""archive: прошедшее из рабочих таблиц → схема archive.

Рабочим таблицам нужно только свежее: извлечение смотрит посты за 30 дней,
склейка — события той же даты, сайт — будущее и ещё 30 дней страницы
«событие прошло». Всё, что старше ARCHIVE_AFTER_DAYS, переносится в схему
archive: рабочие таблицы не растут, история остаётся для аналитики.

Что и когда уходит (по каждому городу, «сегодня» — по его часовому поясу):

1. События каталога с датой старше порога — вместе со всеми упоминаниями.
2. Упоминания из карантина, чей пост опубликован раньше порога.
3. Посты VK и Telegram, опубликованные раньше порога, у которых не осталось
   упоминаний в staging. Пост с анонсом на три месяца вперёд остаётся, пока
   не уйдёт само событие. Последний пост источника не уходит никогда: по нему
   загрузка считает водяной знак, а source_health — молчание источника.
4. Записи журнала извлечения ушедших постов.
5. Показы с сайтов с датой старше порога, если их упоминание уже ушло.
6. Адреса страниц событий с датой старше порога.

Порог больше, чем окно извлечения (extract.MAX_POST_AGE_DAYS) и срок страницы
«событие прошло» (pages.GONE_DAYS): архив не уносит то, что пайплайн ещё может
тронуть. Поэтому rebuild_catalog и reextract архив не видят и не теряют.

Всё — одна транзакция с той же блокировкой каталога, что у dedupe_events:
упадёт посередине — не перенесётся ничего. Каждый шаг — DELETE … RETURNING
внутри INSERT в архив: строка не может оказаться ни в двух местах, ни нигде.
Повторный запуск ничего не делает: переносить уже нечего.

Журнал загрузок meta.ingest_log не архивируется, а чистится: строки старше
LOG_KEEP_DAYS удаляются (trim_ingest_log).
"""

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from funfinder.db import lock_catalog

log = logging.getLogger(__name__)

ARCHIVE_AFTER_DAYS = 60
LOG_KEEP_DAYS = 90

# Рабочая таблица → (архивная, первичный ключ архивной).
TABLES = {
    "staging.events": ("archive.mentions", ("id",)),
    "catalog.events": ("archive.events", ("event_id",)),
    "raw.vk_posts": ("archive.vk_posts", ("owner_id", "post_id")),
    "raw.tg_posts": ("archive.tg_posts", ("source_id", "post_id")),
    "staging.post_extractions": ("archive.post_extractions", ("source_id", "post_id")),
    "raw.site_events": ("archive.site_events", ("id",)),
    "catalog.event_pages": ("archive.event_pages", ("city_id", "slug")),
}

CITY_SOURCES = "SELECT source_id FROM meta.sources WHERE city_id = %(city_id)s"

# Шаги в порядке выполнения: (название для статистики, рабочая таблица, условие на строку t).
# Порядок важен: упоминания уходят раньше своих событий (внешний ключ) и раньше
# постов (пост уходит, только когда упоминаний не осталось).
STEPS = [
    (
        "mentions",
        "staging.events",
        "t.event_id IN (SELECT event_id FROM catalog.events WHERE city_id = %(city_id)s AND event_date < %(cutoff)s)",
    ),
    (
        "events",
        "catalog.events",
        """t.city_id = %(city_id)s AND t.event_date < %(cutoff)s
           AND NOT EXISTS (SELECT 1 FROM staging.events s WHERE s.event_id = t.event_id)""",
    ),
    (
        "quarantined",
        "staging.events",
        """t.city_id = %(city_id)s AND t.event_id IS NULL AND t.validated
           AND t.quarantine_reason IS NOT NULL AND t.published_at < %(posts_before)s""",
    ),
    (
        "vk_posts",
        "raw.vk_posts",
        f"""t.source_id IN ({CITY_SOURCES}) AND t.published_at < %(posts_before)s
           AND NOT EXISTS (SELECT 1 FROM staging.events s WHERE s.source_id = t.source_id AND s.post_id = t.post_id)
           AND t.post_id < (SELECT max(p.post_id) FROM raw.vk_posts p WHERE p.owner_id = t.owner_id)""",
    ),
    (
        "tg_posts",
        "raw.tg_posts",
        f"""t.source_id IN ({CITY_SOURCES}) AND t.published_at < %(posts_before)s
           AND NOT EXISTS (SELECT 1 FROM staging.events s WHERE s.source_id = t.source_id AND s.post_id = t.post_id)
           AND t.post_id < (SELECT max(p.post_id) FROM raw.tg_posts p WHERE p.source_id = t.source_id)""",
    ),
    (
        "post_extractions",
        "staging.post_extractions",
        f"""t.source_id IN ({CITY_SOURCES})
           AND NOT EXISTS (SELECT 1 FROM raw.posts p WHERE p.source_id = t.source_id AND p.post_id = t.post_id)""",
    ),
    (
        "site_events",
        "raw.site_events",
        f"""t.source_id IN ({CITY_SOURCES}) AND t.event_date < %(cutoff)s
           AND NOT EXISTS (SELECT 1 FROM staging.events s WHERE s.source_id = t.source_id AND s.post_id = t.id)""",
    ),
    (
        "event_pages",
        "catalog.event_pages",
        "t.city_id = %(city_id)s AND t.event_date < %(cutoff)s",
    ),
]


def columns(cur, table: str) -> list[str]:
    schema, name = table.split(".")
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
         WHERE table_schema = %s AND table_name = %s
         ORDER BY ordinal_position
        """,
        (schema, name),
    )
    return [row[0] for row in cur.fetchall()]


def move_sql(table: str, cols: list[str], where: str) -> str:
    """DELETE из рабочей таблицы и INSERT удалённого в архивную — одной командой.

    Столбцы берутся из рабочей таблицы: если в архивной какого-то нет, команда
    упадёт, а не перенесёт строку без него. Строка, которая уже есть в архиве
    (пост удалили из raw, а потом он вернулся), заменяется свежей.
    """
    target, key = TABLES[table]
    col_list = ", ".join(cols)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in key)
    return f"""
        WITH moved AS (DELETE FROM {table} t WHERE {where} RETURNING t.*)
        INSERT INTO {target} ({col_list})
        SELECT {col_list} FROM moved
        ON CONFLICT ({", ".join(key)}) DO UPDATE SET {updates}, archived_at = now()
    """


def run(conn, *, now: datetime | None = None, keep_days: int = ARCHIVE_AFTER_DAYS) -> dict:
    now = now or datetime.now(UTC)
    stats = dict.fromkeys((name for name, _, _ in STEPS), 0)
    with conn, conn.cursor() as cur:
        lock_catalog(cur)
        cols = {table: columns(cur, table) for table in TABLES}
        cur.execute("SELECT city_id, slug, timezone FROM meta.cities ORDER BY city_id")
        for city_id, slug, tz in cur.fetchall():
            params = {
                "city_id": city_id,
                "cutoff": now.astimezone(ZoneInfo(tz)).date() - timedelta(days=keep_days),
                "posts_before": now - timedelta(days=keep_days),
            }
            for name, table, where in STEPS:
                cur.execute(move_sql(table, cols[table], where), params)
                stats[name] += cur.rowcount
            log.info("%s: в архив до %s", slug, params["cutoff"])
    log.info("Перенесено в архив: %s", stats)
    return stats


def trim_ingest_log(conn, *, now: datetime | None = None, keep_days: int = LOG_KEEP_DAYS) -> dict:
    now = now or datetime.now(UTC)
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM meta.ingest_log WHERE started_at < %s", (now - timedelta(days=keep_days),))
        deleted = cur.rowcount
    log.info("Из журнала загрузок удалено строк старше %d дней: %d", keep_days, deleted)
    return {"deleted": deleted}
