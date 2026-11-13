"""Сайт: сборка страниц из каталога и логика выкладки. Без сети; сборка из базы — на настоящем Postgres."""

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import paramiko
import pytest

from funfinder.site import build, deploy
from funfinder.site.build import Event

MSK = ZoneInfo("Europe/Moscow")
CITY = {"city_id": 1, "slug": "lipetsk", "name": "Липецк", "timezone": "Europe/Moscow"}
CATEGORIES = {"concert": "Концерты", "theatre": "Театр", "classes": "Занятия и кружки", "other": "Другое"}


def event(n, day, at=None, category="concert", **kw):
    fields = {
        "event_id": n,
        "title": f"Событие {n}",
        "day": day,
        "at": at,
        "venue": "Театр драмы",
        "price": None,
        "category": category,
        "category_name": CATEGORIES[category],
        "status": "scheduled",
        "url": f"https://vk.com/wall-1_{n}",
    }
    fields.update(kw)
    return Event(**fields)


def page(out, path):
    return (out / path / "index.html").read_text(encoding="utf-8")


# --- Мелочи ---


@pytest.mark.parametrize(
    ("city", "expected"),
    [("Липецк", "Липецке"), ("Москва", "Москве"), ("Казань", "Казани"), ("Тверь", "Твери"), ("Сочи", "Сочи")],
)
def test_locative(city, expected):
    assert build.locative(city) == expected


def test_group_by_day_and_names():
    days = build.group_by_day(
        [event(1, date(2026, 11, 14)), event(2, date(2026, 11, 14)), event(3, date(2026, 11, 15))]
    )
    assert [(d.day.day, len(d.events), d.weekday, d.weekday_short, d.month) for d in days] == [
        (14, 2, "суббота", "сб", "ноября"),
        (15, 1, "воскресенье", "вс", "ноября"),
    ]


def test_json_ld_marks_cancelled_and_cannot_close_script_tag():
    data = build.json_ld(
        [event(1, date(2026, 11, 14), time(19, 0), status="cancelled", title="</script><b>")], city="Липецк", tz=MSK
    )
    assert "<" not in data and ">" not in data
    item = json.loads(data)["@graph"][0]
    assert item["name"] == "</script><b>"  # для поисковика название не изменилось
    assert item["startDate"] == "2026-11-14T19:00:00+03:00"
    assert item["eventStatus"] == "https://schema.org/EventCancelled"
    assert "streetAddress" not in item["location"]["address"]  # адреса нет — поля нет


def test_glue_abbreviations():
    assert build.glue_abbreviations("ул. Фрунзе, д. 1") == "ул.\u00a0Фрунзе, д.\u00a01"
    assert build.glue_abbreviations(None) is None


def test_json_ld_street_address_from_venue_directory():
    item = json.loads(build.json_ld([event(1, date(2026, 11, 14), address="ул. Ленина, 25")], city="Липецк", tz=MSK))
    assert item["@graph"][0]["location"]["address"]["streetAddress"] == "ул. Ленина, 25"


# --- Сборка страниц ---


@pytest.fixture
def site(tmp_path):
    day = date(2026, 11, 14)
    events = [
        event(
            1,
            day,
            time(19, 0),
            title="Концерт <script>alert(1)</script>",
            price="от 500 ₽",
            address="пл. Театральная, 1",
        ),
        event(2, day, None, category="theatre", status="cancelled", venue=None),
        event(3, day, time(20, 30), category="classes", title="Танго / Группа B"),
    ]
    out = tmp_path / "site"
    stats = build.render(
        out,
        [CITY],
        CATEGORIES,
        {"lipetsk": events},
        site_url="https://matingu.com/",
        contact="https://vk.me/matingu",
        now=datetime(2026, 11, 13, 21, 15, tzinfo=MSK),
    )
    return out, stats


def test_render_writes_city_category_and_service_pages(site):
    out, stats = site
    assert stats == {"pages": 4, "events": 3}  # город + концерты, театр, занятия
    for path in ("index.html", "404.html", "robots.txt", "sitemap.xml", "assets/site.css", "assets/site.js"):
        assert (out / path).exists(), path
    assert (out / "assets/fonts/podkova-cyrillic-wght-normal.woff2").exists()
    assert not (out / "lipetsk/other").exists()  # пустых категорий нет


def test_city_page_content(site):
    out, _ = site
    html = page(out, "lipetsk")
    assert "<h1>Куда сходить в Липецке</h1>" in html
    assert "Обновлено 13 ноября в 21:15" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html  # в разметке — как текст
    assert "<script>alert" not in html  # и нигде не как тег, в том числе в JSON-LD
    assert "Отменено" in html and "Место уточняйте в анонсе" in html
    assert "Концерты, от 500 ₽" in html
    assert 'Театр драмы<span class="event-address">, пл. Театральная, 1</span>' in html  # адрес из справочника
    assert "Танго" not in html  # регулярные занятия не забивают главную
    assert 'href="https://vk.me/matingu">vk.me/matingu</a>' in html
    assert re.search(r'<a href="/lipetsk/classes/">Занятия и кружки</a>', html)  # но в навигации есть


def test_category_page(site):
    out, _ = site
    html = page(out, "lipetsk/classes")
    assert "<h1>Занятия и кружки в Липецке</h1>" in html
    assert "Танго / Группа B" in html
    assert 'href="/lipetsk/classes/" aria-current="page"' in html
    assert '<link rel="canonical" href="https://matingu.com/lipetsk/classes/">' in html


def test_sitemap_and_robots(site):
    out, _ = site
    sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
    assert sitemap.count("<loc>") == 4 and "<loc>https://matingu.com/lipetsk/theatre/</loc>" in sitemap
    assert "Sitemap: https://matingu.com/sitemap.xml" in (out / "robots.txt").read_text(encoding="utf-8")
    assert 'url=/lipetsk/"' in (out / "index.html").read_text(encoding="utf-8")


# --- Сборка из базы ---


def test_run_builds_from_catalog(conn, lipetsk, tmp_path):
    soon = date.today() + timedelta(days=3)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT city_id FROM meta.cities WHERE slug = 'lipetsk'")
        city_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO catalog.events (city_id, title, title_norm, event_date, event_time, venue, category)
            VALUES (%s, 'Обыкновенное чудо', 'обыкн чудо', %s, '19:00', 'Театр драмы', 'theatre'),
                   (%s, 'Без места и времени', 'без места', %s, NULL, NULL, 'other')
            RETURNING event_id
            """,
            (city_id, soon, city_id, soon),
        )
        for (event_id,) in cur.fetchall():
            cur.execute(
                """
                INSERT INTO staging.events (source_id, city_id, owner_id, post_id, source_url, published_at,
                                            title, event_date, validated, event_id)
                VALUES (%s, %s, -1, %s, %s, now(), 'x', %s, true, %s)
                """,
                (lipetsk["source_id"], city_id, event_id, f"https://vk.com/wall-1_{event_id}", soon, event_id),
            )

    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "old.html").write_text("прошлая сборка")
    stats = build.run(conn, tmp_path, site_url="https://matingu.com")

    html = page(tmp_path / "site", "lipetsk")
    assert stats == {"pages": 2, "events": 1}  # событие без времени и места в витрину не попадает
    assert "Обыкновенное чудо" in html and "https://vk.com/wall-1_" in html
    assert not (tmp_path / "site" / "old.html").exists()  # папка подменена целиком
    assert not (tmp_path / "site.new").exists()


# --- Выкладка ---


def test_switch_command_is_atomic_and_keeps_last_releases():
    command = deploy.switch_command("/var/www/matingu", "20261113-213000", keep=5)
    assert "cd /var/www/matingu;" in command
    assert "ln -sfn releases/20261113-213000 current.new; mv -Tf current.new current;" in command
    assert "tail -n +6" in command
    assert "'/var/www/my site'" in deploy.switch_command("/var/www/my site", "1")  # пути экранируются


def test_release_name_sorts_by_time():
    assert deploy.release_name(datetime(2026, 11, 13, 21, 30, 5, tzinfo=UTC)) == "20261113-213005"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])  # utf-16 — так пишет `>` в PowerShell
def test_load_known_hosts(tmp_path, encoding):
    # публичный ключ одноразового тестового сервера
    host_key = "AAAAC3NzaC1lZDI1NTE5AAAAIOPQJjooQG5w6Hr7UiZKV9blc0iiFqzr0DluD0YL+Wzw"
    path = tmp_path / "known_hosts"
    path.write_text(f"# комментарий\n176.124.223.161 ssh-ed25519 {host_key}\n", encoding=encoding)

    client = paramiko.SSHClient()
    deploy.load_known_hosts(client, path)
    assert client.get_host_keys().lookup("176.124.223.161")["ssh-ed25519"].get_base64() == host_key


def test_load_known_hosts_rejects_empty_file(tmp_path):
    path = tmp_path / "known_hosts"
    path.write_text("\n")
    with pytest.raises(ValueError):
        deploy.load_known_hosts(paramiko.SSHClient(), path)


class FakeSFTP:
    def __init__(self):
        self.dirs, self.files = [], []

    def mkdir(self, path):
        self.dirs.append(path)

    def put(self, local, remote):
        self.files.append(remote)


def test_upload_tree_creates_dirs_before_files(tmp_path):
    (tmp_path / "lipetsk" / "theatre").mkdir(parents=True)
    (tmp_path / "index.html").write_text("x")
    (tmp_path / "lipetsk" / "theatre" / "index.html").write_text("x")
    sftp = FakeSFTP()

    assert deploy.upload_tree(sftp, tmp_path, "/var/www/matingu/releases/1") == 2
    assert sftp.dirs == [
        "/var/www/matingu/releases/1",
        "/var/www/matingu/releases/1/lipetsk",
        "/var/www/matingu/releases/1/lipetsk/theatre",
    ]
    assert sftp.files == [
        "/var/www/matingu/releases/1/index.html",
        "/var/www/matingu/releases/1/lipetsk/theatre/index.html",
    ]
