"""Сайты площадок: разбор афиш театров, robots.txt, загрузка в raw.site_events и перенос в staging."""

from datetime import date, time, timedelta
from pathlib import Path

import pytest

from funfinder.events import dedupe, site_events, validate, venues
from funfinder.sites import teatr48, teatr_tolstogo
from funfinder.sites.base import (
    Listing,
    RobotsDisallowed,
    SiteEvent,
    SiteFormatError,
    clean_title,
    robots_allows,
    robots_rules,
)
from funfinder.sites.ingest import ingest_site, normalize_domain, save_listing
from funfinder.sources import VenueConfig, _sync_venues

FIXTURES = Path(__file__).parent / "fixtures" / "sites"
TODAY = date(2026, 11, 14)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --- Разбор ---


def test_teatr_tolstogo_afisha():
    listing = teatr_tolstogo.parse(fixture("teatr_tolstogo_afisha.html"))  # страница в windows-1251
    assert listing.months == {(2026, 11), (2026, 12)}
    first = listing.events[0]
    assert first == SiteEvent(
        key="834@2026-11-14@11:00",
        title="«Невероятные приключения Элли в волшебной стране»",  # без «6+»
        day=date(2026, 11, 14),
        at=time(11, 0),
        url="https://www.teatr-tolstogo.ru/afisha/spectacles/834.html",
    )
    by_key = {e.key: e for e in listing.events}
    # У «Вестсайдской истории» в образце нет кнопки покупки — дата и время прочитаны из текста.
    assert by_key["832@2026-11-15@18:00"].title == "«Вестсайдская история»"
    assert {"834@2026-12-06@11:00", "834@2026-12-06@14:00"} <= set(by_key)  # два показа в один день
    assert len([e for e in listing.events if e.key.startswith("904@")]) == 15


def test_teatr48_calendar():
    listing = teatr48.parse(fixture("teatr48_main.html"))
    assert listing.months == {(2026, 11)}
    assert len(listing.events) == 10
    nov29 = [e for e in listing.events if e.day == date(2026, 11, 29)]
    assert [(e.at, e.title) for e in nov29] == [
        (time(11, 0), "Красавица и Чудовище"),
        (time(18, 0), "Мужской стриптиз"),
    ]
    assert nov29[1].url == "https://www.teatr48.ru/repertoire/muzhskoj-striptiz.html"  # относительно <base href>
    assert nov29[1].key == "muzhskoj-striptiz@2026-11-29@18:00"


def test_teatr48_respects_robots_for_next_month():
    requested = []

    def get(url):
        requested.append(url)
        if "refreshCalendar" in url:
            raise RobotsDisallowed(url)
        return fixture("teatr48_main.html")

    listing = teatr48.collect(get, TODAY)
    assert requested == ["https://www.teatr48.ru/", teatr48.month_url(2026, 12)]
    assert len(listing.events) == 10 and listing.months == {(2026, 11)}


def test_layout_change_fails_loudly():
    with pytest.raises(SiteFormatError):
        teatr_tolstogo.parse("<html><body>Сайт на реконструкции</body></html>".encode())
    with pytest.raises(SiteFormatError):
        teatr48.parse(b"<html><body></body></html>")


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("«Ханума» 12+", "«Ханума»"),
        ("«Бесприданница» А.Н. Островский (16+)", "«Бесприданница» А.Н. Островский"),
        ('"Танцы на закате"  16+', "«Танцы на закате»"),
        ("НОВОГОДНИЙ КАРНАВАЛ!", "НОВОГОДНИЙ КАРНАВАЛ!"),
        ("Д-р", "Д-р"),
    ],
)
def test_clean_title(raw, clean):
    assert clean_title(raw) == clean


# --- robots.txt ---

MODX_ROBOTS = """
User-agent: Yandex
Disallow: /search
Clean-param: utm_source

User-agent: *
Disallow: /*?   # адреса с параметрами
Disallow: /manager/
Allow: /?page=
"""


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://www.teatr48.ru/", True),
        ("https://www.teatr48.ru/repertoire/zori.html", True),
        ("https://www.teatr48.ru/?action=refreshCalendar&month=12&year=2026", False),
        ("https://www.teatr48.ru/manager/index.php", False),
        ("https://www.teatr48.ru/?page=2", True),  # более длинное разрешающее правило побеждает
        ("https://www.teatr48.ru/search", True),  # это правило — только для Яндекса
    ],
)
def test_robots_rules(url, allowed):
    assert robots_allows(robots_rules(MODX_ROBOTS), url) is allowed


def test_robots_own_group_wins():
    rules = robots_rules("User-agent: *\nDisallow: /\n\nUser-agent: FunFinder\nAllow: /\n")
    assert robots_allows(rules, "https://example.ru/afisha/")


# --- Имена сайтов в yaml ---


@pytest.mark.parametrize("raw", ["teatr48.ru", "https://www.teatr48.ru/", "www.teatr48.ru/repertoire/"])
def test_normalize_domain(raw):
    assert normalize_domain(raw) == "teatr48.ru"


@pytest.mark.parametrize("raw", ["not a site", "example.ru"])  # у example.ru нет разбора
def test_normalize_domain_rejects(raw):
    with pytest.raises(ValueError):
        normalize_domain(raw)


# --- База: raw.site_events и перенос в staging ---


@pytest.fixture
def theatre(conn, lipetsk):
    """Сайт театра — источник в том же городе, с площадкой и категорией из yaml."""
    with conn, conn.cursor() as cur:
        cur.execute("SELECT city_id FROM meta.cities WHERE slug = 'lipetsk'")
        city_id = cur.fetchone()[0]
        ids = _sync_venues(
            cur,
            city_id,
            (VenueConfig("teatr-dramy", "Театр драмы им. Л. Н. Толстого", "пл. Театральная, 2", ("Театр драмы",)),),
        )
        cur.execute(
            """
            INSERT INTO meta.sources (city_id, platform, handle, settings, venue_id)
            VALUES (%s, 'site', 'teatr-tolstogo.ru', '{"category": "theatre"}', %s)
            RETURNING source_id
            """,
            (city_id, ids["teatr-dramy"]),
        )
        return {"source_id": cur.fetchone()[0], "handle": "teatr-tolstogo.ru"}


# Даты в тестах с базой — от сегодняшнего дня: витрина показывает только будущее.
D1 = date.today() + timedelta(days=10)
D2 = D1 + timedelta(days=1)
MONTHS = {(d.year, d.month) for d in (D1, D2)}


def show(day: date, title="«Ханума»", at=time(18, 0)) -> SiteEvent:
    return SiteEvent(
        f"825@{day.isoformat()}@{at:%H:%M}", title, day, at, "https://www.teatr-tolstogo.ru/afisha/spectacles/825.html"
    )


def listing(*shows) -> Listing:
    return Listing(events=list(shows), months=set(MONTHS))


def mentions(conn):
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT title, event_date, event_time, category FROM staging.events ORDER BY event_date, event_time"
        )
        return cur.fetchall()


def test_save_listing_tracks_changes_and_removals(conn, theatre):
    sid = theatre["source_id"]
    first = save_listing(conn, sid, listing(show(D1), show(D2)), run_id="1", today=date.today())
    assert (first.inserted, first.updated, first.removed) == (2, 0, 0)
    assert save_listing(conn, sid, listing(show(D1), show(D2)), run_id="2", today=date.today()).as_dict() == {
        "fetched": 2, "inserted": 0, "updated": 0, "removed": 0,
    }  # fmt: skip

    # 29-е сняли с афиши, у 28-го поменяли название.
    changed = save_listing(conn, sid, listing(show(D1, title="«Ханума» (премьера)")), run_id="3", today=date.today())
    assert (changed.updated, changed.removed) == (1, 1)
    # Показ вернулся — отметка снята.
    back = save_listing(
        conn, sid, listing(show(D1, title="«Ханума» (премьера)"), show(D2)), run_id="4", today=date.today()
    )
    assert back.updated == 1
    with conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.site_events WHERE removed_at IS NOT NULL")
        assert cur.fetchone()[0] == 0


def test_month_outside_listing_is_not_removed(conn, theatre):
    sid = theatre["source_id"]
    save_listing(conn, sid, listing(show(D1)), run_id="1", today=date.today())
    # Страница показывает уже другой месяц: показ из месяца, которого на ней нет, не трогаем.
    later = D2 + timedelta(days=40)
    result = save_listing(conn, sid, Listing([], {(later.year, later.month)}), run_id="2", today=date.today())
    assert result.removed == 0


def test_site_shows_flow_into_catalog_and_merge_with_posts(conn, lipetsk, theatre):
    save_listing(conn, theatre["source_id"], listing(show(D1), show(D2)), run_id="1", today=date.today())
    assert site_events.run(conn) == {"loaded": 2, "removed": 0}
    assert site_events.run(conn) == {"loaded": 0, "removed": 0}  # повторно ничего не переносим
    assert mentions(conn) == [
        ("«Ханума»", D1, time(18, 0), "theatre"),
        ("«Ханума»", D2, time(18, 0), "theatre"),
    ]

    # Анонс того же показа из паблика: место написано по-своему, название без кавычек.
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO staging.events (source_id, city_id, post_id, source_url, published_at, title, event_date,
                                        event_time, venue, category)
            SELECT %s, city_id, 1, 'https://vk.com/wall-1_1', now(), 'Ханума', %s, '18:00',
                   'Театр драмы', 'theatre'
              FROM meta.cities WHERE slug = 'lipetsk'
            """,
            (lipetsk["source_id"], D1),
        )
    validate.run(conn)
    venues.run(conn)
    stats = dedupe.run(conn)
    assert (stats["created"], stats["linked"]) == (2, 1)  # пост прилип к показу с сайта
    with conn, conn.cursor() as cur:
        cur.execute("SELECT title, event_date, venue, mentions FROM catalog.events_public ORDER BY event_date")
        rows = cur.fetchall()
    assert rows[0][1:] == (D1, "Театр драмы им. Л. Н. Толстого", 2)

    # Показ 29-го сняли — упоминание уходит, событие удаляет склейка.
    save_listing(conn, theatre["source_id"], listing(show(D1)), run_id="2", today=date.today())
    assert site_events.run(conn) == {"loaded": 0, "removed": 1}
    assert dedupe.run(conn)["deleted"] == 1


def test_ingest_site_uses_the_parser(conn, theatre):
    class Fetcher:
        def get(self, url):
            assert url == teatr_tolstogo.URL
            return fixture("teatr_tolstogo_afisha.html")

    result = ingest_site(Fetcher(), conn, theatre, run_id="1", today=TODAY)
    assert (result.fetched, result.inserted) == (21, 21)
