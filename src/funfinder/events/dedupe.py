"""dedupe_events: проверенные, но ещё не склеенные упоминания → catalog.events.

Новое упоминание сравнивается с событиями каталога того же города на ту же
дату — в том числе с теми, что появились в прошлых запусках (анонс в
понедельник, напоминание в пятницу). Похожее нашлось — упоминание
привязывается к событию. Нет — создаётся новое событие. Затем поля события
пересчитываются из всех его упоминаний.

Похожесть: время отличается не больше чем на TIME_TOLERANCE_MIN минут (или
у одного из двух оно не указано) и похожее название после нормализации.

rebuild=True пересобирает каталог с нуля из staging.events — без вызовов LLM.
Нужно после смены правил склейки: старые события каталога были склеены
по старым правилам, и их title_norm посчитан по-старому.
"""

import logging
import re
from collections import Counter

from rapidfuzz import fuzz

log = logging.getLogger(__name__)

THRESHOLD = 80  # порог похожести названий, 0..100
TIME_TOLERANCE_MIN = 30  # Прачечная пишет 19:01, а перепосты округляют до 19:00
STEM_LEN = 5  # грубая «основа» слова: «Султана Лагучева» и «Султан Лагучев» → «султа лагуч»

# Общие слова-типы, которые не отличают одно событие от другого.
_TYPE_WORDS = re.compile(
    r"\b(игра|квиз|концерт|спектакль|выставка|лекция|вечеринка|мастер класс|кинопоказ|новинка|премьера"
    r"|выступление|сольный)\b"
)


def norm_title(title: str | None) -> str:
    t = (title or "").lower().replace("ё", "е")
    t = re.sub(r"\(.*?\)", " ", t)  # (Новинка!), (Вход 400р.)
    t = re.sub(r"[«»\"'!?.,:;/—–-]", " ", t)  # кавычки и пунктуация
    t = _TYPE_WORDS.sub(" ", t)
    return " ".join(word[:STEM_LEN] for word in t.split())  # без окончаний: падежи не мешают


def title_score(a: str, b: str) -> float:
    # token_set_ratio даёт 100, если одно название целиком входит в другое.
    # Для названий из одного слова («вау») это опасно, поэтому их сравниваем строго.
    if min(len(a.split()), len(b.split())) < 2:
        return fuzz.ratio(a, b)
    return fuzz.token_set_ratio(a, b)


def times_compatible(a, b) -> bool:
    if a is None or b is None:
        return True
    return abs((a.hour * 60 + a.minute) - (b.hour * 60 + b.minute)) <= TIME_TOLERANCE_MIN


def _longest(values):
    return max((v for v in values if v), key=len, default=None)


def _most_common(values, *, skip=None):
    counted = Counter(v for v in values if v and v != skip)
    return counted.most_common(1)[0][0] if counted else skip


def recompute_event(cur, event_id: int) -> None:
    """Пересчитывает поля события из всех его упоминаний."""
    cur.execute(
        """
        SELECT title, event_time, venue, price, category, status
          FROM staging.events
         WHERE event_id = %s
         ORDER BY published_at, id
        """,
        (event_id,),
    )
    items = cur.fetchall()
    title = _most_common(i[0] for i in items)
    cur.execute(
        """
        UPDATE catalog.events
           SET title = %s, title_norm = %s, event_time = %s, venue = %s, price = %s,
               category = %s, status = %s, updated_at = now()
         WHERE event_id = %s
        """,
        (
            title,
            norm_title(title),
            next((i[1] for i in items if i[1] is not None), None),  # время: первое известное
            _longest(i[2] for i in items),  # место: самое подробное
            _longest(i[3] for i in items),
            _most_common((i[4] for i in items), skip="other"),  # «другое» — только если иначе никак
            items[-1][5],  # статус: из самого свежего поста
            event_id,
        ),
    )


def run(conn, *, rebuild: bool = False) -> dict:
    created = linked = 0
    touched: set[int] = set()
    candidates: dict[tuple, list[dict]] = {}  # (city_id, date) → события каталога

    with conn, conn.cursor() as cur:  # одна транзакция: упадёт — ничего не склеится наполовину
        if rebuild:
            # В той же транзакции, что и склейка: читатели витрины не увидят пустой каталог.
            cur.execute("UPDATE staging.events SET event_id = NULL WHERE event_id IS NOT NULL")
            cur.execute("DELETE FROM catalog.events")
            log.info("Каталог очищен, пересобираю из staging: удалено событий %d", cur.rowcount)

        # Сначала упоминания с известным временем, потом без: так «безвременные»
        # присоединяются к уже известным сеансам, а не становятся отдельными событиями.
        cur.execute(
            """
            SELECT id, city_id, title, event_date, event_time, venue, price, category, status
              FROM staging.events
             WHERE validated AND quarantine_reason IS NULL AND event_id IS NULL
             ORDER BY event_time IS NULL, published_at, id
            """
        )
        rows = cur.fetchall()

        for row_id, city_id, title, event_date, event_time, venue, price, category, status in rows:
            tnorm = norm_title(title)
            key = (city_id, event_date)
            if key not in candidates:
                cur.execute(
                    """
                    SELECT event_id, title_norm, event_time
                      FROM catalog.events
                     WHERE city_id = %s AND event_date = %s
                    """,
                    key,
                )
                candidates[key] = [{"event_id": e, "title_norm": n, "time": t} for e, n, t in cur.fetchall()]

            match = next(
                (
                    c
                    for c in candidates[key]
                    if times_compatible(c["time"], event_time) and title_score(c["title_norm"], tnorm) >= THRESHOLD
                ),
                None,
            )
            if match is None:
                cur.execute(
                    """
                    INSERT INTO catalog.events
                           (city_id, title, title_norm, event_date, event_time, venue, price, category, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING event_id
                    """,
                    (city_id, title, tnorm, event_date, event_time, venue, price, category, status),
                )
                event_id = cur.fetchone()[0]
                candidates[key].append({"event_id": event_id, "title_norm": tnorm, "time": event_time})
                created += 1
            else:
                event_id = match["event_id"]
                match["time"] = match["time"] or event_time  # чтобы другой сеанс в этом запуске не прилип
                linked += 1

            cur.execute("UPDATE staging.events SET event_id = %s WHERE id = %s", (event_id, row_id))
            touched.add(event_id)

        for event_id in touched:
            recompute_event(cur, event_id)

        # События без упоминаний: пост отредактировали, и событие из него пропало.
        cur.execute(
            """
            DELETE FROM catalog.events e
             WHERE NOT EXISTS (SELECT 1 FROM staging.events s WHERE s.event_id = e.event_id)
            """
        )
        deleted = cur.rowcount

    return {"mentions": len(rows), "created": created, "linked": linked, "deleted": deleted}
