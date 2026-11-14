"""Постоянные адреса страниц событий: латиница, раздача адресов между сборками, склейки и пропажи."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest

from funfinder.site.pages import GONE_DAYS, PageRow, assign, make_slug, slugify, visible_gone

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
DAY = date(2026, 11, 14)


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Престиж", "prestizh"),
        ("Киноклуб: «Престиж» (2006)", "kinoklub-prestizh-2006"),
        ("Щёлкунчик", "shchelkunchik"),
        ("Объявление сбора", "obyavlenie-sbora"),
        ("Хор. Царь-колокол", "khor-tsar-kolokol"),
        ("DJ insane & gypsy kid", "dj-insane-gypsy-kid"),
        ("🎄✨", "sobytie"),
        (
            "Очень длинное название концерта, которое никак не помещается в адрес страницы",
            "ochen-dlinnoe-nazvanie-kontserta-kotoroe-nikak-ne",
        ),
    ],
)
def test_slugify(title, slug):
    assert slugify(title) == slug
    assert len(slug) <= 50


def test_make_slug_avoids_collisions():
    taken = {"2026-11-14-prestizh", "2026-11-14-prestizh-2"}
    assert make_slug(DAY, "Престиж", taken) == "2026-11-14-prestizh-3"


@dataclass
class Ev:
    event_id: int
    title: str
    day: date = DAY
    venue: str | None = None
    venue_slug: str | None = None
    url: str = "https://vk.com/wall-1_1"
    slug: str | None = None


def build(events, rows=None, now=NOW):
    """Одна сборка: адреса раздаются, как в build.render. Возвращает адреса после сборки."""
    return assign(events, rows or [], now=now)


def rebuild(rows, events, now=NOW + timedelta(hours=3)):
    """Следующая сборка: адреса из базы (created_at уже есть), события — с новыми номерами."""
    for i, row in enumerate(rows):
        row.created_at = row.created_at or NOW + timedelta(seconds=i)
    return assign(events, rows, now=now)


def test_new_events_get_readable_slugs():
    events = [Ev(1, "Престиж"), Ev(2, "Мгла", day=DAY + timedelta(days=7))]
    rows = build(events)
    assert [e.slug for e in events] == ["2026-11-14-prestizh", "2026-11-21-mgla"]
    assert {r.slug for r in rows} == {"2026-11-14-prestizh", "2026-11-21-mgla"}


def test_slug_survives_new_event_id_and_title_tweak():
    rows = build([Ev(1, "Киноклуб: Престиж")])
    event = Ev(77, "Престиж. Киноклуб в Синице")  # каталог пересобран, название из другого поста
    rows = rebuild(rows, [event])
    assert event.slug == "2026-11-14-kinoklub-prestizh"
    assert len(rows) == 1 and rows[0].title == "Престиж. Киноклуб в Синице"


def test_same_titles_at_different_venues_keep_their_own_pages():
    first = [Ev(1, "Новогодний концерт", venue_slug="filarmoniya"), Ev(2, "Новогодний концерт", venue_slug="ocknt")]
    rows = build(first)
    slugs = {e.venue_slug: e.slug for e in first}
    assert slugs == {"filarmoniya": "2026-11-14-novogodniy-kontsert", "ocknt": "2026-11-14-novogodniy-kontsert-2"}

    # В следующей сборке события пришли в другом порядке и с другими номерами — адреса не перепутались.
    second = [Ev(9, "Новогодний концерт", venue_slug="ocknt"), Ev(8, "Новогодний концерт", venue_slug="filarmoniya")]
    rebuild(rows, second)
    assert {e.venue_slug: e.slug for e in second} == slugs


def test_merged_events_redirect_to_the_older_page():
    rows = build([Ev(1, "Престиж"), Ev(2, "Престиж (2006)", venue_slug="sinitsa")])
    # Склейка: вторую запись дедупликация признала тем же событием.
    merged = Ev(5, "Престиж", venue_slug="sinitsa")
    rows = rebuild(rows, [merged])
    by_slug = {r.slug: r for r in rows}
    assert merged.slug == "2026-11-14-prestizh"
    assert by_slug["2026-11-14-prestizh-2006"].redirect_to == "2026-11-14-prestizh"


def test_gone_pages_live_for_a_while():
    rows = build([Ev(1, "Престиж")])
    rows = rebuild(rows, [])  # события больше нет
    assert rows[0].event_id is None and rows[0].redirect_to is None
    assert visible_gone(rows[0], DAY)  # снято с афиши
    assert visible_gone(rows[0], DAY + timedelta(days=GONE_DAYS))  # прошло, но ещё показываем
    assert not visible_gone(rows[0], DAY + timedelta(days=GONE_DAYS + 1))


def test_dissimilar_titles_do_not_share_a_page():
    rows = build([Ev(1, "Престиж")])
    other = Ev(2, "Мгла")
    rebuild(rows, [other])
    assert other.slug == "2026-11-14-mgla"


def test_page_row_keeps_last_seen_for_gone_events():
    row = PageRow("2026-11-14-prestizh", DAY, "Престиж", "прест", created_at=NOW, last_seen_at=NOW)
    later = NOW + timedelta(days=1)
    assign([], [row], now=later)
    assert row.last_seen_at == NOW  # событие не видели — отметка прежняя
