"""validate_quality: проверка новых строк staging.events.

Плохое не удаляется, а получает quarantine_reason: так видно, что и почему
отбраковано, а правило можно поменять и перепроверить.
"""

# Порядок важен: строка получает первую подошедшую причину.
RULES = [
    ("no_title", "s.title IS NULL"),
    ("no_date", "s.event_date IS NULL"),
    # Событие раньше даты публикации — фотоотчёт или итоги: модель их иногда пропускает.
    ("past_event", "s.event_date < (s.published_at AT TIME ZONE c.timezone)::date"),
    # Больше года вперёд — почти наверняка модель ошиблась с годом.
    ("date_too_far", "s.event_date > (s.published_at AT TIME ZONE c.timezone)::date + 366"),
]


def run(conn) -> dict:
    stats = {}
    with conn, conn.cursor() as cur:
        for reason, condition in RULES:
            cur.execute(
                f"""
                UPDATE staging.events s
                   SET quarantine_reason = %s
                  FROM meta.cities c
                 WHERE c.city_id = s.city_id
                   AND NOT s.validated
                   AND s.quarantine_reason IS NULL
                   AND {condition}
                """,
                (reason,),
            )
            stats[reason] = cur.rowcount

        # Категория не из справочника — не повод выкидывать событие, просто «другое».
        cur.execute(
            """
            UPDATE staging.events
               SET category = 'other'
             WHERE NOT validated
               AND (category IS NULL OR category NOT IN (SELECT slug FROM meta.categories))
            """
        )
        stats["category_fixed"] = cur.rowcount

        cur.execute("UPDATE staging.events SET validated = true WHERE NOT validated")
        stats["checked"] = cur.rowcount

    stats["passed"] = stats["checked"] - sum(stats[reason] for reason, _ in RULES)
    return stats
