"""publish: выбор и текст подборки, Telegram без сети, журнал выпусков на настоящем Postgres."""

import json
import subprocess
import sys
import threading
from datetime import UTC, date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from funfinder.events.dedupe import norm_title
from funfinder.publish import digest, run, telegram
from funfinder.publish.digest import Digest, DigestSettings, Item, Section
from funfinder.site import build
from funfinder.sources import SourcesConfigError, load_city_config, normalize_tg_chat, sync_city

TOKEN = "123456:SECRET-token"
TODAY = date(2026, 11, 14)
TOMORROW = TODAY + timedelta(days=1)


# --- Настройки и адреса ---


def test_plural():
    assert [digest.plural(n, digest.EVENTS) for n in (1, 2, 5, 11, 12, 21, 22, 25, 111)] == [
        "событие", "события", "событий", "событий", "событий", "событие", "события", "событий", "событий",
    ]  # fmt: skip


def test_digest_settings():
    assert DigestSettings.from_overrides(None).exclude_categories == ("classes",)
    assert DigestSettings.from_overrides({"exclude_categories": ["sport"]}).exclude_categories == ("sport",)
    for bad in ({"exclude": ["x"]}, {"exclude_categories": "classes"}):
        with pytest.raises(ValueError):
            DigestSettings.from_overrides(bad)


@pytest.mark.parametrize(
    ("raw", "chat"),
    [
        ("@matingu_test", "@matingu_test"),
        ("matingu_test", "@matingu_test"),
        ("https://t.me/matingu_test/", "@matingu_test"),
        ("-1001234567890", "-1001234567890"),
    ],
)
def test_normalize_tg_chat(raw, chat):
    assert normalize_tg_chat(raw) == chat


@pytest.mark.parametrize("raw", ["@ab", "@имя", "matingu test", "@1abc"])
def test_normalize_tg_chat_rejects(raw):
    with pytest.raises(ValueError):
        normalize_tg_chat(raw)


# --- На какой день подборка ---


def msk(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC) - timedelta(hours=3)


def test_plan_evening_run_is_for_tomorrow():
    p = run.plan(msk(TODAY, 20), msk(TODAY, 20, 1), "Europe/Moscow")
    assert (p.issue_date, p.today, p.late) == (TOMORROW, TODAY, False)


def test_plan_run_after_midnight_is_late():
    # Компьютер выключили до восьми вечера, включили утром: пропущенный запуск выполнится, но «завтра» уже наступило.
    assert run.plan(msk(TODAY, 20), msk(TOMORROW, 9), "Europe/Moscow").late
    assert not run.plan(msk(TODAY, 20), msk(TODAY, 23, 50), "Europe/Moscow").late  # до полуночи — успеваем


def test_plan_manual_date_is_never_late():
    p = run.plan(msk(TODAY, 20), msk(TOMORROW, 9), "Europe/Moscow", override=TODAY)
    assert (p.issue_date, p.late) == (TODAY, False)


# --- Выбор событий ---


def ev(event_id, title, at=None, category="concert", status="scheduled", day=TOMORROW, venue="Клуб", price=None):
    return build.Event(
        event_id=event_id, title=title, day=day, at=at, venue=venue, price=price, category=category,
        category_name=category, status=status, url=f"https://vk.com/wall-1_{event_id}",
    )  # fmt: skip


CATEGORIES = {"concert": "Концерты", "theatre": "Театр", "exhibition": "Выставки", "classes": "Занятия и кружки"}


def test_select_orders_filters_and_links():
    events = [
        ev(1, "Выставка", None, "exhibition"),
        ev(2, "Поздний концерт", time(21)),
        ev(3, "Ранний концерт", time(18), price="500 ₽"),
        ev(4, "Концерт без времени", None),
        ev(5, "Спектакль", time(19), "theatre", price="Билеты от 300 до 1500 рублей, для пенсионеров скидка"),
        ev(6, "Танго, группа B", time(20), "classes"),  # регулярные занятия — не в подборку
        ev(7, "Отменили", time(19), status="cancelled"),
        ev(8, "Послезавтра", time(19), day=TOMORROW + timedelta(days=1)),
    ]
    links = {3: "https://m.com/lipetsk/events/2026-11-15-rannii/"}
    sections = digest.select(events, TOMORROW, CATEGORIES, DigestSettings(), links)
    assert [(s.name, [i.title for i in s.items]) for s in sections] == [
        ("Концерты", ["Ранний концерт", "Поздний концерт", "Концерт без времени"]),
        ("Театр", ["Спектакль"]),
        ("Выставки", ["Выставка"]),
    ]
    early = sections[0].items[0]
    assert (early.url, early.price) == ("https://m.com/lipetsk/events/2026-11-15-rannii/", "500 ₽")
    assert sections[0].items[1].url is None  # страницы нет — нет и ссылки
    assert sections[1].items[0].price is None  # длинная «цена» — это уже абзац, не цена


def test_select_uses_short_venue_names():
    events = [ev(1, "Квартирник", time(19)), ev(2, "Джаз", time(20))]
    events[0].venue, events[0].venue_slug = "Галерея современного искусства «Буксир»", "buksir"
    events[1].venue, events[1].venue_slug = "Гастробар «Дача»", "dacha"  # короткого имени нет — полное
    (section,) = digest.select(events, TOMORROW, CATEGORIES, DigestSettings(), {}, short_names={"buksir": "«Буксир»"})
    assert [i.venue for i in section.items] == ["«Буксир»", "Гастробар «Дача»"]


def test_link_stays_on_the_platform_of_the_digest():
    at = datetime(2026, 11, 10, 12, tzinfo=UTC)
    rows = [
        (1, "https://vk.com/wall-1_10", at),
        (1, "https://t.me/sinitsabar/2671", at),  # программа на месяц
        (1, "https://t.me/sinitsabar/2700", at + timedelta(days=3)),  # «Сегодня в 20:00» — свежее
        (2, "https://vk.com/wall-1_11", at),
        (3, "https://www.teatr-tolstogo.ru/afisha/spectacles/825.html", at),
    ]
    assert digest.same_platform_posts(rows, "tg") == {1: "https://t.me/sinitsabar/2700"}
    assert digest.same_platform_posts(rows, "vk") == {1: "https://vk.com/wall-1_10", 2: "https://vk.com/wall-1_11"}

    pages = {1: "2026-11-15-kino", 2: "2026-11-15-kviz"}
    links = digest.choose_links(digest.same_platform_posts(rows, "tg"), pages, "https://matingu.com/lipetsk/events/")
    assert links == {
        1: "https://t.me/sinitsabar/2700",  # в Telegram — сразу в канал, без захода на сайт
        2: "https://matingu.com/lipetsk/events/2026-11-15-kviz/",  # поста в Telegram нет — страница на сайте
    }


# --- Текст ---


def sample(n_items: int = 2, title: str = "Квиз «Кино»") -> Digest:
    items = [
        Item(title=title, at=time(19), venue="Гастробар «Дача»", price="500 ₽", url="https://m.com/lipetsk/events/a/"),
        Item(title="Выставка <без> ссылки & времени", at=None, venue=None, price=None, url=None),
    ]
    items += [Item(f"Событие {i}", time(12), "Площадка с длинным названием", None, None) for i in range(n_items - 2)]
    return Digest("Липецк", TOMORROW, "https://matingu.com/lipetsk/", [Section("Квизы", items)])


def test_render_html():
    text, shown = digest.render(sample(), today=TODAY, as_html=True, limit=digest.TG_LIMIT)
    assert shown == 2
    assert text.splitlines()[:2] == ["<b>Завтра в Липецке</b>", "воскресенье, 15 ноября"]
    assert '19:00 <a href="https://m.com/lipetsk/events/a/">Квиз «Кино»</a> — Гастробар «Дача», 500 ₽' in text
    assert "Выставка &lt;без&gt; ссылки &amp; времени" in text  # текст из постов не становится разметкой
    assert text.endswith('<a href="https://matingu.com/lipetsk/">Вся афиша — matingu.com</a>')


def test_render_plain_for_vk():
    text, _ = digest.render(sample(), today=TODAY, as_html=False, limit=digest.PLAIN_LIMIT)
    assert "\nКВИЗЫ\n19:00 Квиз «Кино» — Гастробар «Дача», 500 ₽\n" in text
    assert "<" not in text.replace("<без>", "")
    assert text.endswith("Вся афиша: https://matingu.com/lipetsk/")


@pytest.mark.parametrize(("today", "title"), [(TODAY, "Завтра в Липецке"), (TOMORROW, "Сегодня в Липецке")])
def test_heading_depends_on_the_day(today, title):
    assert digest.heading(sample(), today) == title
    assert digest.heading(sample(), TODAY - timedelta(days=3)) == "Куда сходить в Липецке"


def test_long_digest_is_cut_with_a_link_to_the_site():
    big = sample(200)
    text, shown = digest.render(big, today=TODAY, as_html=True, limit=digest.TG_LIMIT)
    assert 2 < shown < 200
    rest = 200 - shown
    assert f"…и ещё {rest} {digest.plural(rest, digest.EVENTS)} на сайте" in text
    assert digest.visible_length(text) <= digest.TG_LIMIT


# --- Telegram ---


class FakeTransport:
    def __init__(self, status=200, body=None, exc=None):
        self.status, self.exc = status, exc
        self.body = body or json.dumps({"ok": True, "result": {"message_id": 42}})
        self.calls = []

    def call(self, token, method, params):
        self.calls.append((token, method, params))
        if self.exc:
            raise self.exc
        return self.status, self.body

    def close(self):
        pass


def test_bot_sends_html_without_previews():
    transport = FakeTransport()
    assert telegram.Bot(TOKEN, transport).send("@matingu_test", "<b>текст</b>") == "42"
    _, method, params = transport.calls[0]
    assert method == "sendMessage"
    assert params == {
        "chat_id": "@matingu_test", "text": "<b>текст</b>", "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }  # fmt: skip


@pytest.mark.parametrize(
    ("status", "body", "retryable", "fragment"),
    [
        (400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}, False, "chat not found"),
        (403, {"ok": False, "error_code": 403, "description": "Forbidden: bot is not a member"}, False, "403"),
        (
            429,
            {"ok": False, "error_code": 429, "description": "Too Many", "parameters": {"retry_after": 7}},
            True,
            "7 с",
        ),
        (502, {"ok": False, "error_code": 502, "description": "Bad Gateway"}, True, "502"),
    ],
)
def test_bot_errors(status, body, retryable, fragment):
    with pytest.raises(telegram.TelegramError) as err:
        telegram.Bot(TOKEN, FakeTransport(status, json.dumps(body))).send("@c", "x")
    assert err.value.retryable is retryable
    assert fragment in str(err.value)


def test_token_never_appears_in_errors():
    # Ошибка, в текст которой попал токен (например, из URL), уходит в лог уже без него.
    leaky = FakeTransport(
        exc=telegram.TelegramError(f"https://api.telegram.org/bot{TOKEN}/sendMessage: boom", retryable=True)
    )
    with pytest.raises(telegram.TelegramError) as err:
        telegram.Bot(TOKEN, leaky).send("@c", "x")
    assert TOKEN not in str(err.value) and "***" in str(err.value)

    class Broken(requests.Session):
        def post(self, url, **kwargs):
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    with pytest.raises(telegram.TelegramError) as err:
        telegram.DirectTransport(Broken()).call(TOKEN, "sendMessage", {})
    assert TOKEN not in str(err.value) and err.value.retryable


def test_missing_token_is_a_config_error():
    with pytest.raises(telegram.TelegramError) as err:
        telegram.Bot("", FakeTransport())
    assert not err.value.retryable


class FakeChannel:
    def __init__(self, status):
        self.status, self.closed_write = status, False

    def shutdown_write(self):
        self.closed_write = True

    def recv_exit_status(self):
        return self.status


class FakeStream:
    def __init__(self, data=b"", channel=None):
        self.data, self.channel, self.written = data, channel, ""

    def write(self, text):
        self.written += text

    def read(self):
        return self.data


class FakeSSH:
    def __init__(self, out: dict, status=0):
        self.out, self.status, self.commands, self.stdin = out, status, [], None

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        channel = FakeChannel(self.status)
        self.stdin = FakeStream(channel=channel)
        return self.stdin, FakeStream(json.dumps(self.out).encode(), channel), FakeStream(b"oops")

    def close(self):
        pass


def test_ssh_transport_keeps_the_token_out_of_the_command():
    ssh = FakeSSH({"status": 200, "body": json.dumps({"ok": True, "result": {"message_id": 7}})})
    assert telegram.Bot(TOKEN, telegram.SSHTransport(ssh)).send("@c", "привет") == "7"
    assert TOKEN not in ssh.commands[0]  # аргументы команды видны в списке процессов сервера
    sent = json.loads(ssh.stdin.written)
    assert (sent["token"], sent["method"], sent["params"]["text"]) == (TOKEN, "sendMessage", "привет")
    assert ssh.stdin.channel.closed_write  # иначе скрипт ждал бы конца ввода вечно


def test_ssh_transport_network_error_on_server_is_retryable():
    with pytest.raises(telegram.TelegramError) as err:
        telegram.SSHTransport(FakeSSH({"error": "URLError: timed out"})).call(TOKEN, "sendMessage", {})
    assert err.value.retryable and "timed out" in str(err.value)


class TelegramStub(BaseHTTPRequestHandler):
    """Локальный «api.telegram.org» для настоящего серверного скрипта."""

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ok = self.path.endswith("/sendMessage") and body.get("chat_id") == "@good"
        answer = (
            {"ok": True, "result": {"message_id": 99}}
            if ok
            else {"ok": False, "error_code": 400, "description": "Bad Request"}
        )
        data = json.dumps(answer).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_url():
    server = HTTPServer(("127.0.0.1", 0), TelegramStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def remote(stub_url, chat):
    """Скрипт, который выполняется на сервере, — здесь, тем же python3 и с тем же stdin."""
    request = {"base": stub_url, "token": TOKEN, "method": "sendMessage", "params": {"chat_id": chat, "text": "x"}}
    done = subprocess.run(
        [sys.executable, "-c", telegram.REMOTE_SCRIPT],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_remote_script_returns_answers_and_errors(stub_url):
    good = remote(stub_url, "@good")
    assert telegram._parse(good["status"], good["body"]) == {"message_id": 99}
    bad = remote(stub_url, "@bad")
    assert bad["status"] == 400  # ошибка HTTP — не исключение скрипта, а ответ с кодом
    with pytest.raises(telegram.TelegramError):
        telegram._parse(bad["status"], bad["body"])
    unreachable = remote("http://127.0.0.1:9", "@good")
    assert "error" in unreachable and TOKEN not in unreachable["error"]


# --- yaml ---


CITY = """
city: {slug: lipetsk, name: Липецк, timezone: Europe/Moscow}
publish:
  - platform: tg
    chat: https://t.me/matingu_test
    note: Тестовый канал
    settings: {exclude_categories: [classes, sport]}
"""


def write(tmp_path, body):
    path = tmp_path / "lipetsk.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_publish_section_is_loaded_and_synced(conn, tmp_path):
    cfg = load_city_config(write(tmp_path, CITY))
    assert [(t.platform, t.handle, t.note) for t in cfg.publish] == [("tg", "@matingu_test", "Тестовый канал")]
    assert sync_city(conn, cfg)["publish_targets"] == 1
    sync_city(conn, cfg)  # повтор ничего не ломает
    sync_city(conn, load_city_config(write(tmp_path, CITY.split("publish:")[0])))  # убрали из yaml
    with conn, conn.cursor() as cur:
        cur.execute("SELECT handle, is_active, settings FROM meta.publish_targets")
        assert cur.fetchall() == [("@matingu_test", False, {"exclude_categories": ["classes", "sport"]})]


@pytest.mark.parametrize(
    "section",
    [
        "publish: [{platform: vk, chat: '123'}]",  # VK пока не подключён
        "publish: [{platform: tg}]",
        "publish: [{platform: tg, chat: 'not a channel'}]",
        "publish: [{platform: tg, chat: '@chan_a', settings: {foo: 1}}]",
        "publish: [{platform: tg, chat: '@chan_a'}, {platform: tg, chat: 'chan_a'}]",
    ],
)
def test_bad_publish_section(tmp_path, section):
    with pytest.raises(SourcesConfigError):
        load_city_config(write(tmp_path, CITY.split("publish:")[0] + section))


# --- Журнал выпусков на базе ---


class FakeSender:
    def __init__(self, fail: telegram.TelegramError | None = None):
        self.fail, self.sent = fail, []

    def send(self, handle, text):
        if self.fail:
            raise self.fail
        self.sent.append((handle, text))
        return f"m{len(self.sent)}"


@pytest.fixture
def target(conn, lipetsk):
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.publish_targets (city_id, platform, handle)
            SELECT city_id, 'tg', '@matingu_test' FROM meta.cities WHERE slug = 'lipetsk'
            RETURNING target_id, city_id
            """
        )
        target_id, city_id = cur.fetchone()
    return {
        "target_id": target_id, "platform": "tg", "handle": "@matingu_test", "settings": {},
        "city_id": city_id, "city_slug": "lipetsk", "city_name": "Липецк", "timezone": "Europe/Moscow",
    }  # fmt: skip


def add_events(conn, source, rows):
    """События каталога с упоминанием: (название, день, время, категория, статус)."""
    with conn, conn.cursor() as cur:
        cur.execute("SELECT city_id FROM meta.cities WHERE slug = 'lipetsk'")
        city_id = cur.fetchone()[0]
        for title, day, at, category, status in rows:
            cur.execute(
                """
                INSERT INTO catalog.events (city_id, title, title_norm, event_date, event_time, venue, category, status)
                VALUES (%s, %s, %s, %s, %s, 'Клуб «Атмосфера»', %s, %s) RETURNING event_id
                """,
                (city_id, title, norm_title(title), day, at, category, status),
            )
            event_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO staging.events (source_id, city_id, post_id, source_url, published_at,
                                            title, event_date, validated, event_id)
                VALUES (%s, %s, %s, %s, now(), %s, %s, true, %s)
                """,
                (source["source_id"], city_id, event_id, f"https://vk.com/wall-1_{event_id}", title, day, event_id),
            )


# Витрина показывает только будущее, поэтому здесь даты — от настоящего «сегодня».
REAL_TODAY = date.today()
REAL_TOMORROW = REAL_TODAY + timedelta(days=1)
ISSUE = run.Plan(REAL_TOMORROW, REAL_TODAY, late=False)


def publish(conn, target, sender, **kw):
    return run.publish(conn, target, sender, issue=ISSUE, site_url="https://matingu.com", run_id="r1", **kw)


def log_row(conn, target):
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, events, message_id, error FROM meta.publish_log WHERE target_id = %s",
            (target["target_id"],),
        )
        return cur.fetchone()


def test_publish_once_then_skip(conn, lipetsk, target, tmp_path):
    add_events(
        conn,
        lipetsk,
        [
            ("Обыкновенное чудо", REAL_TOMORROW, time(19), "theatre", "scheduled"),
            ("Квиз «Кино»", REAL_TOMORROW, time(20), "quiz", "scheduled"),
            ("Танго, группа B", REAL_TOMORROW, time(20), "classes", "scheduled"),
            ("Отменённый концерт", REAL_TOMORROW, time(19), "concert", "cancelled"),
            ("Послезавтра", REAL_TOMORROW + timedelta(days=1), time(19), "concert", "scheduled"),
        ],
    )
    build.run(conn, tmp_path, site_url="https://matingu.com")  # страницы событий опубликованы — будут ссылки

    sender = FakeSender()
    result = publish(conn, target, sender)
    assert (result["status"], result["events"], result["message_id"]) == ("sent", 2, "m1")
    chat, text = sender.sent[0]
    assert chat == "@matingu_test"
    assert text.startswith("<b>Завтра в Липецке</b>")
    slug = f"{REAL_TOMORROW.isoformat()}-obyknovennoe-chudo"
    assert f'<a href="https://matingu.com/lipetsk/events/{slug}/">Обыкновенное чудо</a>' in text
    assert "Танго" not in text and "Отменённый" not in text and "Послезавтра" not in text
    assert log_row(conn, target) == ("sent", 2, "m1", None)

    # Повторный запуск того же вечера ничего не публикует.
    assert publish(conn, target, sender)["status"] == "already_sent"
    assert len(sender.sent) == 1
    # А с force — публикует (тестовый канал).
    assert publish(conn, target, sender, force=True)["message_id"] == "m2"


def test_telegram_digest_links_to_the_telegram_post(conn, lipetsk, target, tmp_path):
    add_events(
        conn,
        lipetsk,
        [
            ("Киноклуб: «Мгла»", REAL_TOMORROW, time(19), "cinema", "scheduled"),
            ("Обыкновенное чудо", REAL_TOMORROW, time(19), "theatre", "scheduled"),
        ],
    )
    with conn, conn.cursor() as cur:  # у киноклуба есть ещё два поста в Telegram-канале
        cur.execute("SELECT event_id, city_id FROM catalog.events WHERE title LIKE 'Киноклуб%%'")
        event_id, city_id = cur.fetchone()
        for post_id, days_ago in ((2671, 5), (2700, 1)):
            cur.execute(
                """
                INSERT INTO staging.events (source_id, city_id, post_id, source_url, published_at,
                                            title, event_date, validated, event_id)
                VALUES (%s, %s, %s, %s, now() - make_interval(days => %s), 'Мгла', %s, true, %s)
                """,
                (lipetsk["source_id"], city_id, post_id, f"https://t.me/sinitsabar/{post_id}", days_ago,
                 REAL_TOMORROW, event_id),
            )  # fmt: skip
    build.run(conn, tmp_path, site_url="https://matingu.com")

    sender = FakeSender()
    publish(conn, target, sender)
    text = sender.sent[0][1]
    assert '<a href="https://t.me/sinitsabar/2700">Киноклуб: «Мгла»</a>' in text  # свежий пост, не сайт
    slug = f"{REAL_TOMORROW.isoformat()}-obyknovennoe-chudo"
    assert f'<a href="https://matingu.com/lipetsk/events/{slug}/">Обыкновенное чудо</a>' in text


def test_no_links_before_the_site_has_pages(conn, lipetsk, target):
    add_events(conn, lipetsk, [("Обыкновенное чудо", REAL_TOMORROW, time(19), "theatre", "scheduled")])
    sender = FakeSender()
    publish(conn, target, sender)
    assert "Обыкновенное чудо" in sender.sent[0][1]
    assert "/events/" not in sender.sent[0][1]  # сайт эту страницу ещё не собирал


def test_failed_send_is_logged_and_retried(conn, lipetsk, target):
    add_events(conn, lipetsk, [("Обыкновенное чудо", REAL_TOMORROW, time(19), "theatre", "scheduled")])
    with pytest.raises(run.PublishError) as err:
        publish(conn, target, FakeSender(telegram.TelegramError("Telegram 502: Bad Gateway", retryable=True)))
    assert err.value.retryable
    assert log_row(conn, target)[0::3] == ("failed", "Telegram 502: Bad Gateway")
    assert publish(conn, target, FakeSender())["status"] == "sent"  # ретрай публикует


def test_interrupted_send_is_not_repeated(conn, lipetsk, target):
    add_events(conn, lipetsk, [("Обыкновенное чудо", REAL_TOMORROW, time(19), "theatre", "scheduled")])
    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meta.publish_log (target_id, issue_date, status) VALUES (%s, %s, 'sending')",
            (target["target_id"], REAL_TOMORROW),
        )
    sender = FakeSender()
    with pytest.raises(run.PublishError) as err:
        publish(conn, target, sender)
    assert not err.value.retryable and "force" in str(err.value)
    assert sender.sent == []
    assert publish(conn, target, sender, force=True)["status"] == "sent"


def test_short_venue_name_comes_from_the_directory(conn, lipetsk, target):
    add_events(conn, lipetsk, [("Обыкновенное чудо", REAL_TOMORROW, time(19), "theatre", "scheduled")])
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.venues (city_id, slug, name, short_name)
            SELECT city_id, 'teatr-dramy', 'Театр драмы им. Л. Н. Толстого', 'Театр драмы'
              FROM meta.cities WHERE slug = 'lipetsk' RETURNING venue_id
            """
        )
        cur.execute("UPDATE catalog.events SET venue_id = %s", (cur.fetchone()[0],))
    sender = FakeSender()
    publish(conn, target, sender)
    assert "19:00 Обыкновенное чудо — Театр драмы\n" in sender.sent[0][1]


def test_empty_day_is_skipped(conn, lipetsk, target):
    sender = FakeSender()
    assert publish(conn, target, sender)["status"] == "skipped"
    assert sender.sent == []
    assert log_row(conn, target)[:2] == ("skipped", 0)
