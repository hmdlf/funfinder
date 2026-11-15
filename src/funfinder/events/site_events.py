"""load_site_events: показы с сайтов площадок (raw.site_events) → упоминания в staging.events.

Показ с сайта — уже готовое событие, LLM не нужна. Категория — та, что указал
сам сайт (у ОЦКНТ — тег карточки), иначе категория сайта из yaml. Упоминание получает
post_id = raw.site_events.id: у сайта нет постов, но пара (source_id, post_id)
остаётся уникальной, и дальше показ идёт тем же путём, что и посты: проверка,
площадка (площадка источника из yaml), склейка с анонсами из пабликов.

Строка переносится, если её содержимое изменилось с прошлого переноса
(loaded_hash). Снятый показ — упоминание удаляется; если других упоминаний у
события нет, его удалит dedupe_events.
"""

import logging

log = logging.getLogger(__name__)

REMOVED = "removed"

SELECT_CHANGED = """
SELECT r.id, r.source_id, src.city_id, r.title, r.event_date, r.event_time, r.url, r.loaded_at,
       coalesce(r.category, src.settings ->> 'category', 'other'),
       CASE WHEN r.removed_at IS NULL THEN r.content_hash ELSE %(removed)s END AS state
  FROM raw.site_events r
  JOIN meta.sources src USING (source_id)
 WHERE r.loaded_hash IS DISTINCT FROM CASE WHEN r.removed_at IS NULL THEN r.content_hash ELSE %(removed)s END
   AND r.event_date >= current_date - 1  -- прошедшие показы переносить незачем
 ORDER BY r.id
"""

INSERT_MENTION = """
INSERT INTO staging.events
       (source_id, city_id, post_id, source_url, published_at, title, event_date, event_time, category, status)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'scheduled')
"""


def run(conn) -> dict:
    loaded = removed = 0
    with conn, conn.cursor() as cur:
        cur.execute(SELECT_CHANGED, {"removed": REMOVED})
        for row_id, source_id, city_id, title, day, at, url, seen_at, category, state in cur.fetchall():
            cur.execute("DELETE FROM staging.events WHERE source_id = %s AND post_id = %s", (source_id, row_id))
            if state == REMOVED:
                removed += 1
            else:
                cur.execute(INSERT_MENTION, (source_id, city_id, row_id, url, seen_at, title, day, at, category))
                loaded += 1
            cur.execute("UPDATE raw.site_events SET loaded_hash = %s WHERE id = %s", (state, row_id))
    stats = {"loaded": loaded, "removed": removed}
    log.info("Показы с сайтов: %s", stats)
    return stats
