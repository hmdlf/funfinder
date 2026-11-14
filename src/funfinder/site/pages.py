"""Постоянные адреса страниц событий: /<город>/events/<дата>-<название латиницей>/.

Номер события из catalog.events для адреса не годится: при пересборке каталога
номера меняются. Поэтому опубликованные адреса хранятся в catalog.event_pages,
а каждая сборка заново решает, какому событию какой адрес принадлежит:
тот же город и дата, похожее название (как при склейке дублей), площадки не
противоречат друг другу.

Два прохода. Сначала каждому событию — не больше одного старого адреса, самый
похожий и самый старый: так два разных события с похожими названиями не
отнимут адрес друг у друга. Потом оставшиеся подходящие адреса становятся
перенаправлениями: события склеились, а ссылка на любое из них должна жить.

Адрес, у которого события больше нет, ещё GONE_DAYS дней открывает страницу
«событие прошло» (или «снято с афиши»), потом пропадает.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from funfinder.events.dedupe import THRESHOLD, norm_title, title_score

GONE_DAYS = 30
SLUG_TITLE_LIMIT = 50  # длина части с названием: адрес остаётся читаемым

_TRANSLIT = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        "a b v g d e e zh z i y k l m n o p r s t u f kh ts ch sh shch - y - e yu ya".split(),
        strict=True,
    )
)
_TRANSLIT.update({"ъ": "", "ь": ""})


def slugify(text: str, limit: int = SLUG_TITLE_LIMIT) -> str:
    """«Киноклуб: «Престиж» (2006)» → «kinoklub-prestizh-2006». Пусто (одни эмодзи) → «sobytie»."""
    latin = "".join(_TRANSLIT.get(ch, ch) for ch in text.lower())
    slug = re.sub(r"[^a-z0-9]+", "-", latin).strip("-")
    if len(slug) > limit:
        cut = slug[:limit]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else cut  # по границе слова
    return slug or "sobytie"


def make_slug(day: date, title: str, taken: set[str]) -> str:
    base = f"{day.isoformat()}-{slugify(title)}"
    slug, n = base, 2
    while slug in taken:  # одинаковые названия в один день
        slug, n = f"{base}-{n}", n + 1
    return slug


@dataclass
class PageRow:
    slug: str
    event_date: date
    title: str
    title_norm: str
    venue: str | None = None
    venue_slug: str | None = None
    source_url: str | None = None
    redirect_to: str | None = None
    created_at: datetime | None = None  # None — адрес появился в этой сборке
    last_seen_at: datetime | None = None
    event_id: int | None = None  # чьё событие сейчас; только в памяти

    def update_from(self, event, now: datetime) -> None:
        self.title, self.title_norm = event.title, norm_title(event.title)
        self.venue, self.venue_slug, self.source_url = event.venue, event.venue_slug, event.url
        self.redirect_to, self.event_id, self.last_seen_at = None, event.event_id, now


def assign(events: list, rows: list[PageRow], *, now: datetime) -> list[PageRow]:
    """Раздаёт адреса событиям одного города: ставит event.slug, возвращает все адреса (старые и новые).

    events — объекты с полями event_id, day, title, venue, venue_slug, url и slug.
    """
    rows_by_date: dict[date, list[PageRow]] = defaultdict(list)
    for row in rows:
        row.event_id = None
        rows_by_date[row.event_date].append(row)
    events_by_date: dict[date, list] = defaultdict(list)
    for event in events:
        events_by_date[event.day].append(event)

    taken = {row.slug for row in rows}
    result = list(rows)
    for day, day_events in events_by_date.items():
        pairs = []
        for row in rows_by_date.get(day, []):
            for event in day_events:
                if row.venue_slug and event.venue_slug and row.venue_slug != event.venue_slug:
                    continue  # разные площадки — разные события, как при склейке
                score = title_score(row.title_norm, norm_title(event.title))
                if score >= THRESHOLD:
                    oldest_first = row.created_at.timestamp() if row.created_at else float("inf")
                    # Порядок: похожее название, затем старый адрес (его уже знают поисковики), затем та же площадка.
                    pairs.append((-score, oldest_first, row.venue_slug != event.venue_slug, row.slug, event, row))
        pairs.sort(key=lambda p: (p[0], p[1], p[2], p[3], p[4].event_id))

        primary: dict[int, PageRow] = {}
        used: set[str] = set()
        for *_, event, row in pairs:  # первый проход: событию — один адрес
            if row.slug not in used and event.event_id not in primary:
                primary[event.event_id] = row
                used.add(row.slug)
        for *_, event, row in pairs:  # второй: лишние подходящие адреса — перенаправления
            if row.slug in used:
                continue
            used.add(row.slug)
            if event.event_id in primary:
                row.redirect_to = primary[event.event_id].slug
            else:
                primary[event.event_id] = row

        for event in day_events:
            row = primary.get(event.event_id)
            if row is None:
                row = PageRow(slug=make_slug(day, event.title, taken), event_date=day, title="", title_norm="")
                taken.add(row.slug)
                result.append(row)
            row.update_from(event, now)
            event.slug = row.slug
    return result


def visible_gone(row: PageRow, today: date) -> bool:
    """Адрес без события: показываем страницу «прошло» ещё GONE_DAYS дней после даты события."""
    return row.event_id is None and row.event_date >= today - timedelta(days=GONE_DAYS)


SELECT_ROWS = """
SELECT slug, event_date, title, title_norm, venue, venue_slug, source_url, redirect_to, created_at, last_seen_at
  FROM catalog.event_pages
 WHERE city_id = %s AND event_date >= %s
"""

UPSERT_ROW = """
INSERT INTO catalog.event_pages
       (city_id, slug, event_date, title, title_norm, venue, venue_slug, source_url, redirect_to, last_seen_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()))
ON CONFLICT (city_id, slug) DO UPDATE
   SET title = EXCLUDED.title, title_norm = EXCLUDED.title_norm, venue = EXCLUDED.venue,
       venue_slug = EXCLUDED.venue_slug, source_url = EXCLUDED.source_url,
       redirect_to = EXCLUDED.redirect_to, last_seen_at = EXCLUDED.last_seen_at
"""


def load_rows(cur, city_id: int, today: date) -> list[PageRow]:
    """Адреса, которые ещё могут понадобиться: события не старше GONE_DAYS дней."""
    cur.execute(SELECT_ROWS, (city_id, today - timedelta(days=GONE_DAYS)))
    return [PageRow(*row) for row in cur.fetchall()]


def save_rows(cur, city_id: int, rows: list[PageRow]) -> None:
    for r in rows:
        cur.execute(
            UPSERT_ROW,
            (
                city_id,
                r.slug,
                r.event_date,
                r.title,
                r.title_norm,
                r.venue,
                r.venue_slug,
                r.source_url,
                r.redirect_to,
                r.last_seen_at,
            ),
        )
