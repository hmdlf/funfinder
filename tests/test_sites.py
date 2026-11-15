"""Сайты площадок: разбор афиш театров, robots.txt, загрузка в raw.site_events и перенос в staging."""

import re
from datetime import date, time, timedelta
from pathlib import Path

import pytest

from funfinder.events import dedupe, site_events, validate, venues
from funfinder.sites import ocknt, teatr48, teatr_tolstogo
from funfinder.sites.base import (
    Listing,
    RobotsDisallowed,
    SiteEvent,
    SiteFormatError,
    clean_title,
    robots_allows,
    robots_rules,
)
from funfinder.sites.ingest import content_hash, ingest_site, normalize_domain, save_listing
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
        ocknt.parse("<html><body>Сайт на реконструкции</body></html>".encode())


def test_ocknt_afisha():
    events, last = ocknt.parse(fixture("ocknt_afisha.html"))
    assert last == 7  # «?PAGEN_1=7» — ссылка «Последняя»
    assert len(events) == 12
    first = events[0]
    assert first.key == "spektakl-schaste-po-prigovoru@2026-11-09@19:00"
    assert (first.title, first.day, first.at) == ("Спектакль «Счастье по приговору»", date(2026, 11, 9), time(19, 0))
    assert first.url == "https://ocknt.ru/afisha/spektakl-schaste-po-prigovoru/"
    by_title = {e.title: e.category for e in events}
    assert by_title["Папа может"] == "cinema"
    assert by_title["Султан Лагучев"] == "concert"
    assert by_title["Мастер-класс «Магия натурального воска»"] == "workshop"
    assert by_title["Спектакль «Собачье сердце»"] == "theatre"


# Unix-время из ответа сервера ocknt.ru 15.11.2026 (лог ingest_sites) — для тех же 12 карточек,
# что в сохранённой из браузера странице: там скрипт уже превратил его в «09 ноября 2026, 19:00».
OCKNT_SERVER_TIMES = [
    1794240000, 1794733200, 1794758400, 1795017600, 1795104000, 1795168800,
    1795251600, 1795276800, 1795363200, 1795442400, 1795449600, 1795536000,
]  # fmt: skip


def test_ocknt_server_html_has_unix_time():
    """Сервер отдаёт в карточке Unix-время, а не текст: разбор даёт те же даты, что видит браузер."""
    page = fixture("ocknt_afisha.html").decode()
    stamps = iter(OCKNT_SERVER_TIMES)
    server = re.sub(r'(date-active-from">)[^<]*(<)', lambda m: f"{m.group(1)}{next(stamps)}{m.group(2)}", page)
    assert next(stamps, None) is None  # заменили ровно 12 дат
    from_server, _ = ocknt.parse(server.encode())
    from_browser, _ = ocknt.parse(page.encode())
    assert [(e.key, e.day, e.at) for e in from_server] == [(e.key, e.day, e.at) for e in from_browser]


@pytest.mark.parametrize(
    ("raw", "when"),
    [
        ("1794240000", (date(2026, 11, 9), time(19, 0))),
        ("1794240000000", (date(2026, 11, 9), time(19, 0))),  # миллисекунды
        ("1794171600", (date(2026, 11, 9), None)),  # полночь по Москве — время не указано
        ("99999999999999999999", None),
    ],
)
def test_ocknt_timestamp(raw, when):
    assert ocknt._when(raw) == when


def ocknt_page(cards, last: int) -> bytes:
    """Страница афиши ОЦКНТ в той же разметке: cards — (slug, название, «25 ноября 2026, 19:00», тег)."""
    items = "".join(
        f"""<li><div class="container-cards-affiche">
             <a href="https://ocknt.ru/afisha/{slug}/"><h3 class="card-title">{title}</h3></a>
             <div class="container-info"><div class="date-active-from">{when}</div>
               <div><div><img src="/i/user.svg"> 12+</div><div><img src="/i/tag.svg"> {tag}</div></div></div>
           </div></li>"""
        for slug, title, when, tag in cards
    )
    pages = "".join(
        f'<a class="pagination-item" href="https://ocknt.ru/afisha/?PAGEN_1={n}">{n}</a>' for n in range(2, last + 1)
    )
    return f'<ul class="grid-wrapper">{items}</ul><div class="pagination-container">{pages}</div>'.encode()


def test_ocknt_reads_all_pages():
    requested = []

    def get(url):
        requested.append(url)
        if url == ocknt.URL:
            return fixture("ocknt_afisha.html")
        n = int(url.rsplit("=", 1)[1])
        return ocknt_page([(f"show-{n}", f"Концерт {n}", f"{n} декабря 2026, 19:00", "Концерт")], last=7)

    listing = ocknt.collect(get, TODAY)
    assert requested == [ocknt.page_url(n) for n in range(1, 8)]
    assert len(listing.events) == 12 + 6
    assert listing.months == {(2026, 11), (2026, 12)}  # дочитали до конца — все месяцы целиком


def test_ocknt_stops_where_robots_says():
    def get(url):
        if url == ocknt.URL:
            return fixture("ocknt_afisha.html")
        if url == ocknt.page_url(2):
            return ocknt_page([("late", "Концерт", "2 декабря 2026, 19:00", "Концерт")], last=7)
        raise RobotsDisallowed(url)

    listing = ocknt.collect(get, TODAY)
    assert len(listing.events) == 13
    # Последний прочитанный показ — в декабре: декабрь мог прочитаться не весь, снятыми его показы не считаем.
    assert listing.months == {(2026, 11)}


def test_ocknt_only_first_page_allowed():
    def get(url):
        if url == ocknt.URL:
            return fixture("ocknt_afisha.html")
        raise RobotsDisallowed(url)

    listing = ocknt.collect(get, TODAY)
    assert len(listing.events) == 12
    assert listing.months == set()  # ноябрь на первой странице не целиком
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


def test_category_from_the_site_wins_over_yaml(conn, theatre):
    sid = theatre["source_id"]
    concert = SiteEvent(f"gala@{D1}@19:00", "Гала-концерт", D1, time(19), "https://ocknt.ru/afisha/gala/", "concert")
    save_listing(conn, sid, Listing(events=[show(D1), concert], months=set(MONTHS)), run_id="1", today=date.today())
    site_events.run(conn)
    assert sorted(m[3] for m in mentions(conn)) == ["concert", "theatre"]  # без категории — категория сайта из yaml


def test_content_hash_without_category_is_unchanged():
    # Иначе после обновления все показы театров разом выглядели бы изменёнными.
    assert content_hash("Ханума", D1, time(18), "u") == content_hash("Ханума", D1, time(18), "u", None)
    assert content_hash("Ханума", D1, time(18), "u") != content_hash("Ханума", D1, time(18), "u", "concert")


def test_ingest_site_uses_the_parser(conn, theatre):
    class Fetcher:
        def get(self, url):
            assert url == teatr_tolstogo.URL
            return fixture("teatr_tolstogo_afisha.html")

    result = ingest_site(Fetcher(), conn, theatre, run_id="1", today=TODAY)
    assert (result.fetched, result.inserted) == (21, 21)
