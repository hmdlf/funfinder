"""Публикация подборки в VK: вход в VK ID, хранение и ротация ключей, запись на стене, формат текста."""

import html
import json
import threading
import time as _time
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import parse_qs, urlparse

import psycopg2
import pytest
import requests
from cryptography.fernet import Fernet

from conftest import ISSUE, REAL_TOMORROW, FakeSender, add_events
from funfinder.publish import digest, run, telegram, tg_chats, vk, vk_auth, vkid
from funfinder.publish.digest import Digest, Item, Section
from funfinder.sources import SourcesConfigError, load_city_config, normalize_vk_group, sync_city
from funfinder.vk.client import VKAccessError, VKAuthError, VKError

NOW = datetime(2026, 11, 15, 17, 0, tzinfo=UTC)
KEY = Fernet.generate_key().decode()


# --- PKCE, адрес входа, адрес возврата ---


def test_code_challenge_matches_rfc7636():
    # Пример из RFC 7636, приложение B.
    assert vkid.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_login_url():
    login = vkid.start_login("54321", "http://localhost/vk-auth")
    assert len(login.state) >= 32 and len(login.verifier) >= 43
    assert set(login.state + login.verifier) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
    url = urlparse(login.url())
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == vkid.AUTHORIZE_URL
    assert query == {
        "response_type": "code",
        "client_id": "54321",
        "scope": "wall groups",
        "redirect_uri": "http://localhost/vk-auth",
        "state": login.state,
        "code_challenge": vkid.code_challenge(login.verifier),
        "code_challenge_method": "S256",
    }
    assert login.verifier not in login.url()  # verifier остаётся у нас


def test_parse_redirect():
    url = "http://localhost/vk-auth?code=vk2.a.c0de&state=S1&device_id=dev-1&expires_in=600&type=code_v2"
    assert vkid.parse_redirect(f"  {url}\n") == {"code": "vk2.a.c0de", "state": "S1", "device_id": "dev-1"}
    payload = json.dumps({"code": "c", "state": "s", "device_id": "d", "type": "code_v2"})
    assert vkid.parse_redirect(f"https://matingu.com/?payload={payload}") == {
        "code": "c",
        "state": "s",
        "device_id": "d",
    }


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("http://localhost/vk-auth?error=access_denied&error_description=User+denied", "access_denied"),
        ("http://localhost/vk-auth?code=c&state=s", "device_id"),
        ("http://localhost/vk-auth", "code"),
    ],
)
def test_parse_redirect_errors(url, fragment):
    with pytest.raises(ValueError, match=fragment):
        vkid.parse_redirect(url)


# --- Запросы к VK ID ---


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeIDSession:
    """id.vk.ru/oauth2/auth: отвечает по очереди из answers; ответ-функция получает запрос."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def post(self, url, data, timeout):
        self.requests.append((url, dict(data)))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer(data) if callable(answer) else answer
        return FakeResponse(status, body)


def tokens_answer(access, refresh, *, expires_in=3600, scope="wall groups", user_id=777):
    """Успешный ответ VK ID: возвращает тот же state, что пришёл в запросе."""

    def answer(data):
        body = {
            "access_token": access, "refresh_token": refresh, "id_token": "jwt", "token_type": "Bearer",
            "expires_in": expires_in, "user_id": user_id, "state": data["state"], "scope": scope,
        }  # fmt: skip
        return 200, body

    return answer


def test_exchange_code():
    login = vkid.start_login("54321", "http://localhost/vk-auth")
    session = FakeIDSession(tokens_answer("acc-1", "ref-1"))
    tokens = vkid.exchange_code(session, login, "vk2.a.c0de", "dev-1", now=NOW)
    url, data = session.requests[0]
    assert url == vkid.TOKEN_URL
    assert data == {
        "grant_type": "authorization_code",
        "code": "vk2.a.c0de",
        "code_verifier": login.verifier,
        "client_id": "54321",
        "device_id": "dev-1",
        "redirect_uri": "http://localhost/vk-auth",
        "state": login.state,
    }
    assert (tokens.access_token, tokens.refresh_token, tokens.user_id) == ("acc-1", "ref-1", 777)
    assert tokens.expires_at == NOW + timedelta(hours=1)
    assert "acc-1" not in repr(tokens) and "ref-1" not in repr(tokens)


@pytest.mark.parametrize(
    ("answer", "error", "retryable"),
    [
        ((400, {"error": "invalid_grant", "error_description": "refresh token is expired"}), vkid.AuthRequired, False),
        ((200, {"error": "invalid_request", "error_description": "invalid code_verifier"}), vkid.AuthRequired, False),
        ((503, ValueError("not json")), vkid.VKIDError, True),
        ((500, {"error": "server_error"}), vkid.VKIDError, True),
        (requests.ConnectionError("https://id.vk.ru/... refresh_token=SECRET"), vkid.VKIDError, True),
        ((200, {"access_token": "a", "refresh_token": "r", "state": "чужой"}), vkid.VKIDError, False),
    ],
)
def test_refresh_errors(answer, error, retryable):
    with pytest.raises(error) as exc:
        vkid.refresh(FakeIDSession(answer), "54321", "dev-1", "SECRET", now=NOW)
    assert exc.value.retryable is retryable
    assert "SECRET" not in str(exc.value)
    if error is vkid.AuthRequired:
        assert "vk_auth" in str(exc.value)  # подсказка, как войти заново


def test_secret_box_supports_key_rotation():
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    encrypted = vkid.secret_box(old).encrypt(b"token")
    assert vkid.secret_box(f"{new},{old}").decrypt(encrypted) == b"token"  # новый шифрует, старый ещё читается
    with pytest.raises(vkid.VKIDError):
        vkid.secret_box("")


# --- Хранение и ротация в базе ---


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def store_factory(conn, db_dsn):
    def make(session, *, key=KEY, clock=None):
        return vkid.TokenStore(lambda: psycopg2.connect(db_dsn), vkid.secret_box(key), session, clock=clock or Clock())

    return make


def stored(conn):
    with conn, conn.cursor() as cur:
        cur.execute("SELECT access_token, refresh_token, access_expires_at, refreshed_at FROM meta.oauth_tokens")
        return cur.fetchone()


def first_login(store, *, expires_in=3600):
    tokens = vkid.Tokens("acc-1", "ref-1", NOW + timedelta(seconds=expires_in), 777, "wall groups")
    store.save("54321", "dev-1", tokens)


def test_tokens_are_stored_encrypted(conn, store_factory):
    store = store_factory(FakeIDSession())
    first_login(store)
    access, refresh, _, _ = stored(conn)
    assert "acc-1" not in access and "ref-1" not in refresh
    assert store.access_token() == "acc-1"  # ключ жив — VK ID не спрашиваем
    info = store.info()
    assert (info["client_id"], info["user_id"], info["scope"], info["refreshed_at"]) == (
        "54321", 777, "wall groups", None,
    )  # fmt: skip
    # Другой ключ шифрования (сменили AIRFLOW_FERNET_KEY) — расшифровать нельзя, нужен новый вход.
    with pytest.raises(vkid.AuthRequired, match="FERNET"):
        store_factory(FakeIDSession(), key=Fernet.generate_key().decode()).access_token()


def test_expired_key_is_refreshed_and_rotated(conn, store_factory):
    clock = Clock()
    session = FakeIDSession(tokens_answer("acc-2", "ref-2"), tokens_answer("acc-3", "ref-3"))
    store = store_factory(session, clock=clock)
    first_login(store)

    clock.now = NOW + timedelta(minutes=56)  # осталось меньше запаса в 5 минут
    assert store.access_token() == "acc-2"
    sent = session.requests[0][1]
    assert {k: sent[k] for k in ("grant_type", "refresh_token", "client_id", "device_id")} == {
        "grant_type": "refresh_token", "refresh_token": "ref-1", "client_id": "54321", "device_id": "dev-1",
    }  # fmt: skip
    assert len(sent["state"]) >= 32
    assert stored(conn)[3] == clock.now
    assert store.access_token() == "acc-2"  # новый ключ ещё жив
    assert len(session.requests) == 1

    clock.now += timedelta(hours=2)
    assert store.access_token() == "acc-3"
    # Обмен идёт по НОВОМУ refresh_token: старый VK уже погасил, повтор отозвал бы сессию.
    assert session.requests[1][1]["refresh_token"] == "ref-2"


def test_failed_refresh_keeps_stored_tokens(conn, store_factory):
    session = FakeIDSession((400, {"error": "invalid_grant"}))
    store = store_factory(session, clock=Clock(NOW + timedelta(hours=2)))
    first_login(store)
    before = stored(conn)
    with pytest.raises(vkid.AuthRequired):
        store.access_token()
    assert stored(conn) == before


def test_no_login_yet(store_factory):
    with pytest.raises(vkid.AuthRequired, match="ещё не выполнен"):
        store_factory(FakeIDSession()).access_token()


class SlowIDSession(FakeIDSession):
    """Обмен, который ждёт сигнала: пока он идёт, вторая задача должна ждать на блокировке строки."""

    def __init__(self, *answers):
        super().__init__(*answers)
        self.entered, self.release = threading.Event(), threading.Event()

    def post(self, url, data, timeout):
        self.entered.set()
        self.release.wait(5)
        return super().post(url, data, timeout)


def test_parallel_tasks_refresh_once(store_factory):
    """Две задачи publish (два города) одновременно видят истёкший ключ: обмен должен быть один."""
    session = SlowIDSession(tokens_answer("acc-2", "ref-2"))
    clock = Clock(NOW + timedelta(hours=2))
    store = store_factory(session, clock=clock)
    first_login(store)

    results = {}
    a = threading.Thread(target=lambda: results.__setitem__("a", store.access_token()))
    a.start()
    assert session.entered.wait(5)
    b = threading.Thread(target=lambda: results.__setitem__("b", store.access_token()))
    b.start()
    _time.sleep(0.3)
    assert "b" not in results  # ждёт на SELECT … FOR UPDATE
    session.release.set()
    a.join(5)
    b.join(5)
    assert results == {"a": "acc-2", "b": "acc-2"}
    assert len(session.requests) == 1  # второй обмен по ref-1 отозвал бы всю сессию


# --- Запись на стене ---


class FakeTokens:
    def __init__(self):
        self.calls = []

    def access_token(self, *, force_refresh=False):
        self.calls.append(force_refresh)
        return f"acc-{len(self.calls)}"


class FakeVK:
    """VKClient: отвечает по методу; ответ — значение, исключение или функция (token, params)."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, token):  # как фабрика клиентов: FakeVK(answers) вместо VKClient
        self.token = token
        return self

    def call(self, method, **params):
        self.calls.append((self.token, method, params))
        answer = self.answers[method]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


GROUP = {"groups": [{"id": 123, "name": "Матингу: тест", "is_admin": 1, "admin_level": 3}]}


def test_wall_post():
    api = FakeVK({"groups.getById": GROUP, "wall.post": {"post_id": 77}})
    wall = vk.Wall(FakeTokens(), client_factory=api)
    message_id = wall.send(
        "matingu_test", "Завтра в Липецке", key="1:2026-11-16:r1", link="https://matingu.com/lipetsk/"
    )
    assert message_id == "wall-123_77"
    (_, m1, p1), (_, m2, p2) = api.calls
    assert (m1, p1) == ("groups.getById", {"group_id": "matingu_test", "fields": "is_admin,admin_level"})
    assert m2 == "wall.post"
    assert p2 == {
        "owner_id": -123,
        "from_group": 1,
        "message": "Завтра в Липецке",
        "attachments": "https://matingu.com/lipetsk/",
        "guid": vk.guid("1:2026-11-16:r1"),
    }
    assert vk.guid("1:2026-11-16:r1") != vk.guid("1:2026-11-16:r2")  # ручной запуск с force — новая запись


@pytest.mark.parametrize(
    ("group", "fragment"),
    [
        ({"groups": []}, "не найдено"),
        ({"groups": [{"id": 123, "name": "Чужое", "is_admin": 0}]}, "не редактор"),
        ({"groups": [{"id": 123, "name": "Модерирую", "is_admin": 1, "admin_level": 1}]}, "не редактор"),
    ],
)
def test_wall_refuses_groups_it_cannot_post_to(group, fragment):
    api = FakeVK({"groups.getById": group, "wall.post": {"post_id": 1}})
    with pytest.raises(vk.VKPublishError, match=fragment) as exc:
        vk.Wall(FakeTokens(), client_factory=api).send("123", "текст")
    assert exc.value.retryable is False
    assert [m for _, m, _ in api.calls] == ["groups.getById"]  # до wall.post не дошло


def test_unknown_admin_status_is_left_to_wall_post():
    group = {"groups": [{"id": 123, "name": "Матингу: тест"}]}  # без is_admin
    api = FakeVK({"groups.getById": group, "wall.post": {"post_id": 9}})
    assert vk.Wall(FakeTokens(), client_factory=api).send("123", "текст") == "wall-123_9"


def test_rejected_key_is_refreshed_once():
    tokens = FakeTokens()
    api = FakeVK({"groups.getById": [VKAuthError(5, "invalid session", "groups.getById"), GROUP],
                  "wall.post": {"post_id": 5}})  # fmt: skip
    assert vk.Wall(tokens, client_factory=api).send("123", "текст") == "wall-123_5"
    assert tokens.calls == [False, True, False]  # второй вызов — с обновлением ключа
    assert [t for t, _, _ in api.calls] == ["acc-1", "acc-2", "acc-3"]


def test_key_rejected_after_refresh_needs_login():
    auth = VKAuthError(5, "invalid session", "groups.getById")
    api = FakeVK({"groups.getById": [auth, auth]})
    with pytest.raises(vk.VKPublishError, match="vk_auth") as exc:
        vk.Wall(FakeTokens(), client_factory=api).send("123", "текст")
    assert exc.value.retryable is False


@pytest.mark.parametrize(
    ("error", "retryable", "fragment"),
    [
        (VKError(9, "Flood control", "wall.post"), True, "Flood"),
        (VKError(10, "Internal server error", "wall.post"), True, "Internal"),
        (VKError(214, "Access to adding post denied", "wall.post"), False, "лимит"),
        (VKAccessError(15, "Access denied", "wall.post"), False, "Стена"),
        (VKError(14, "Captcha needed", "wall.post"), False, "капчу"),
        (requests.ReadTimeout("read timed out"), True, "сеть"),
    ],
)
def test_wall_errors(error, retryable, fragment):
    api = FakeVK({"groups.getById": GROUP, "wall.post": error})
    with pytest.raises(vk.VKPublishError, match=fragment) as exc:
        vk.Wall(FakeTokens(), client_factory=api).send("123", "текст")
    assert exc.value.retryable is retryable


def test_missing_login_is_reported_before_any_call():
    class NoTokens:
        def access_token(self, *, force_refresh=False):
            raise vkid.AuthRequired("вход в VK ID ещё не выполнен")

    api = FakeVK({})
    with pytest.raises(vkid.AuthRequired):
        vk.Wall(NoTokens(), client_factory=api).send("123", "текст")
    assert api.calls == []


# --- Текст для VK ---


TODAY, TOMORROW = date(2026, 11, 14), date(2026, 11, 15)


def vk_sample():
    items = [
        Item("Квиз [Кино|Музыка]", time(19), "Гастробар «Дача»", "500 ₽", "https://vk.com/wall-1_5"),
        Item("Концерт", time(20), "Буксир", None, "https://m.vk.com/wall-2_7?from=feed"),
        Item("Выставка", None, "Музей", None, "https://matingu.com/lipetsk/events/vystavka/"),
    ]
    return Digest("Липецк", TOMORROW, "https://matingu.com/lipetsk/", [Section("Квизы", items)])


def test_render_vk_links_only_to_vk():
    text, shown = digest.render(vk_sample(), today=TODAY, fmt="vk", limit=digest.VK_LIMIT)
    assert shown == 3
    assert text.splitlines()[:2] == ["Завтра в Липецке", "воскресенье, 15 ноября"]
    assert "\nКВИЗЫ\n" in text
    # Скобки и «|» из названия не ломают разметку ссылки.
    assert "19:00 [https://vk.com/wall-1_5|Квиз (Кино/Музыка)] — Гастробар «Дача», 500 ₽" in text
    assert "20:00 [https://vk.com/wall-2_7?from=feed|Концерт] — Буксир" in text  # m.vk.com → vk.com
    assert "\nВыставка — Музей\n" in text  # страница сайта ссылкой-словом в VK не бывает
    assert text.endswith("Вся афиша: https://matingu.com/lipetsk/")


def test_vk_limit_counts_link_markup():
    url = "https://vk.com/wall-123456789_" + "1" * 40
    items = [Item(f"Событие {i}", time(12), "Площадка", None, url) for i in range(300)]
    big = Digest("Липецк", TOMORROW, "https://matingu.com/lipetsk/", [Section("Концерты", items)])
    text, shown = digest.render(big, today=TODAY, fmt="vk", limit=3000)
    assert len(text) <= 3000  # VK считает весь текст записи, вместе с адресами ссылок
    assert 0 < shown < 300 and f"…и ещё {300 - shown}" in text


@pytest.mark.parametrize(
    ("url", "link"),
    [
        ("https://vk.com/wall-1_2", "https://vk.com/wall-1_2"),
        ("https://www.vk.ru/wall-1_2", "https://vk.com/wall-1_2"),
        ("https://t.me/sinitsabar/2700", None),
        ("https://vk.com/", None),
        (None, None),
    ],
)
def test_vk_link(url, link):
    assert digest.vk_link(url) == link


# --- yaml ---


CITY = """
city: {slug: lipetsk, name: Липецк, timezone: Europe/Moscow}
publish:
"""


def write(tmp_path, body):
    path = tmp_path / "lipetsk.yaml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("raw", "handle"),
    [
        ("https://vk.com/club123456", "123456"),
        ("vk.com/public123456", "123456"),
        ("-123456", "123456"),
        ("123456", "123456"),
        ("https://vk.ru/Matingu_Test/", "matingu_test"),
    ],
)
def test_normalize_vk_group(raw, handle):
    assert normalize_vk_group(raw) == handle


def test_vk_publish_section(tmp_path):
    cfg = load_city_config(write(tmp_path, CITY + "  - {platform: vk, group: 'https://vk.com/club123', note: Тест}"))
    assert [(t.platform, t.handle, t.note) for t in cfg.publish] == [("vk", "123", "Тест")]


@pytest.mark.parametrize(
    "items",
    [
        ["{platform: vk}"],
        ["{platform: vk, group: 'https://example.com/x y'}"],
        ["{platform: vk, group: club1}", "{platform: vk, group: '-1'}"],  # одно сообщество дважды
    ],
)
def test_bad_vk_publish_section(tmp_path, items):
    with pytest.raises(SourcesConfigError):
        load_city_config(write(tmp_path, CITY + "".join(f"  - {item}\n" for item in items)))


# --- Выпуск в VK целиком ---


@pytest.fixture
def vk_target(conn, lipetsk):
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.publish_targets (city_id, platform, handle)
            SELECT city_id, 'vk', '123' FROM meta.cities WHERE slug = 'lipetsk'
            RETURNING target_id, city_id
            """
        )
        target_id, city_id = cur.fetchone()
    return {
        "target_id": target_id, "platform": "vk", "handle": "123", "settings": {},
        "city_id": city_id, "city_slug": "lipetsk", "city_name": "Липецк", "timezone": "Europe/Moscow",
    }  # fmt: skip


def test_vk_digest_links_to_vk_posts(conn, lipetsk, vk_target):
    add_events(conn, lipetsk, [("Обыкновенное чудо", REAL_TOMORROW, time(19), "theatre", "scheduled")])
    with conn, conn.cursor() as cur:
        cur.execute("SELECT event_id FROM catalog.events")
        event_id = cur.fetchone()[0]
    sender = FakeSender()
    result = run.publish(conn, vk_target, sender, issue=ISSUE, site_url="https://matingu.com", run_id="r1")
    assert result["status"] == "sent"
    handle, text = sender.sent[0]
    assert handle == "123"
    assert f"19:00 [https://vk.com/wall-1_{event_id}|Обыкновенное чудо] — Клуб «Атмосфера»" in text
    assert "<" not in text  # без HTML


def test_vk_send_error_is_logged(conn, lipetsk, vk_target):
    add_events(conn, lipetsk, [("Концерт", REAL_TOMORROW, time(19), "concert", "scheduled")])
    sender = FakeSender(vk.VKPublishError("VK API wall.post: [214] Access to adding post denied", retryable=False))
    with pytest.raises(run.PublishError) as exc:
        run.publish(conn, vk_target, sender, issue=ISSUE, site_url="https://matingu.com", run_id="r1")
    assert exc.value.retryable is False
    with conn, conn.cursor() as cur:
        cur.execute("SELECT status, error FROM meta.publish_log")
        assert cur.fetchone() == ("failed", "VK API wall.post: [214] Access to adding post denied")


# --- Команда входа ---


def test_login_command(conn, store_factory):
    session = FakeIDSession(tokens_answer("acc-1", "ref-1", scope="vkid.personal_info groups"))
    store = store_factory(session)
    printed = []

    def ask(prompt):
        url = next(line for line in printed if line.startswith(vkid.AUTHORIZE_URL))
        state = parse_qs(urlparse(url).query)["state"][0]
        return f"http://localhost/vk-auth?code=vk2.a.c0de&state={state}&device_id=dev-9&type=code_v2"

    tokens = vk_auth.login(store, "54321", "http://localhost/vk-auth", ask=ask, out=printed.append, now=lambda: NOW)
    assert tokens.user_id == 777
    assert session.requests[0][1]["device_id"] == "dev-9"
    assert store.info()["scope"] == "vkid.personal_info groups"
    assert any("не выдал право wall" in line for line in printed)  # без «Стены» публиковать не выйдет
    assert all("acc-1" not in line and "ref-1" not in line for line in printed)


def test_login_rejects_foreign_state(store_factory):
    store = store_factory(FakeIDSession())
    with pytest.raises(ValueError, match="state"):
        vk_auth.login(
            store, "54321", "http://localhost/vk-auth",
            ask=lambda _: "http://localhost/vk-auth?code=c&state=other&device_id=d", out=lambda _: None,
        )  # fmt: skip


def test_check_groups():
    printed = []

    def wall_factory(store):
        unknown = {"groups": [{"id": 5, "name": "Без статуса"}]}
        return vk.Wall(store, client_factory=FakeVK({"groups.getById": [GROUP, {"groups": []}, unknown]}))

    groups = [("lipetsk", "123"), ("voronezh", "404"), ("elets", "5")]
    assert not vk_auth.check_groups(FakeTokens(), groups, printed.append, wall_factory=wall_factory)
    assert printed[0].startswith("  ✓ lipetsk: Матингу: тест (id 123)")
    assert printed[1].startswith("  ✗ voronezh: 404 — VK: сообщество '404' не найдено")
    assert printed[2].startswith("  ? elets: Без статуса (id 5)")


# --- Полуавтомат: текст для VK приходит в Telegram ---


class TGTransport:
    """Bot API без сети: отвечает result на любой метод, запоминает вызовы."""

    def __init__(self, result):
        self.result, self.calls = result, []

    def call(self, token, method, params):
        self.calls.append((method, params))
        return 200, json.dumps({"ok": True, "result": self.result})

    def close(self):
        pass


@pytest.mark.parametrize(("handle", "url"), [("123", "https://vk.com/club123"), ("matingu", "https://vk.com/matingu")])
def test_group_url(handle, url):
    assert vk.group_url(handle) == url


def test_draft_is_sent_to_telegram_as_copyable_block():
    transport = TGTransport({"message_id": 42})
    draft = vk.Draft(telegram.Bot("1:T", transport), "123456789")
    text = "Завтра в Липецке\n19:00 [https://vk.com/wall-1_5|Квиз <Кино> & Ко] — «Дача»"
    assert draft.send("123", text, key="k", link="https://matingu.com/lipetsk/") == "draft:42"
    method, params = transport.calls[0]
    assert (method, params["chat_id"], params["parse_mode"]) == ("sendMessage", "123456789", "HTML")
    assert params["link_preview_options"] == {"is_disabled": True}
    assert "https://vk.com/club123" in params["text"]
    # Внутри <pre> — ровно текст для VK: после разбора HTML Telegram покажет его как есть.
    block = params["text"].split("<pre>", 1)[1].removesuffix("</pre>")
    assert html.unescape(block) == text


def test_longest_draft_fits_one_telegram_message():
    url = "https://vk.com/wall-123456789_" + "1" * 12
    items = [Item(f"Событие с длинным названием №{i}", time(12), "Площадка", "500 ₽", url) for i in range(300)]
    big = Digest("Липецк", TOMORROW, "https://matingu.com/lipetsk/", [Section("Концерты", items)])
    text, shown = digest.render(big, today=TODAY, fmt="vk", limit=digest.VK_DRAFT_LIMIT)
    assert 0 < shown < 300
    assert digest.visible_length(vk.Draft.message("123456789", text)) <= 4096


def test_vk_draft_section(conn, tmp_path):
    cfg = load_city_config(
        write(tmp_path, CITY + "  - {platform: vk, group: 'vk.com/club123', draft_to: 123456789, note: Тест}\n")
    )
    assert [(t.platform, t.handle, t.draft_chat) for t in cfg.publish] == [("vk", "123", "123456789")]
    sync_city(conn, cfg)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT platform, handle, draft_chat FROM meta.publish_targets")
        assert cur.fetchall() == [("vk", "123", "123456789")]


@pytest.mark.parametrize(
    "item",
    [
        "{platform: tg, chat: '@chan_a', draft_to: 123}",  # полуавтомат — только для VK
        "{platform: vk, group: club1, draft_to: 'не чат'}",
    ],
)
def test_bad_draft_to(tmp_path, item):
    with pytest.raises(SourcesConfigError):
        load_city_config(write(tmp_path, CITY + f"  - {item}\n"))


def test_draft_target_uses_telegram_sized_limit(conn, lipetsk, vk_target, monkeypatch):
    add_events(
        conn,
        lipetsk,
        [(f"Концерт {i}", REAL_TOMORROW, time(19), "concert", "scheduled") for i in range(5)],
    )
    monkeypatch.setattr(digest, "VK_DRAFT_LIMIT", 250)
    sender = FakeSender()
    result = run.publish(
        conn, vk_target | {"draft_chat": "123456789"}, sender, issue=ISSUE, site_url="https://matingu.com", run_id="r1"
    )
    assert result["events"] < result["total"] == 5
    assert len(sender.sent[0][1]) <= 250
    assert "[https://vk.com/wall-1_" in sender.sent[0][1]  # формат VK, не HTML


def test_bot_chats_from_updates():
    updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 555, "type": "private", "first_name": "Игорь", "username": "igor"}},
        },
        {
            "update_id": 2,
            "message": {"chat": {"id": 555, "type": "private", "first_name": "Игорь", "username": "igor"}},
        },
        {"update_id": 3, "my_chat_member": {"chat": {"id": -100777, "type": "channel", "title": "Матингу"}}},
    ]
    transport = TGTransport(updates)
    bot = telegram.Bot("1:T", transport)
    assert bot.chats() == [
        {"id": 555, "type": "private", "name": "Игорь (@igor)"},
        {"id": -100777, "type": "channel", "name": "Матингу"},
    ]
    printed = []
    assert tg_chats.show(bot, printed.append) == 0
    assert any("555" in line and "личный чат: Игорь (@igor)" in line for line in printed)
    assert tg_chats.show(telegram.Bot("1:T", TGTransport([])), printed.append) == 1


def test_make_transport(tmp_path):
    assert isinstance(
        telegram.make_transport(via_ssh=False, host=None, user="deploy", secrets_dir=tmp_path), telegram.DirectTransport
    )
    with pytest.raises(telegram.TelegramError, match="SITE_DEPLOY_HOST") as exc:
        telegram.make_transport(via_ssh=True, host=None, user="deploy", secrets_dir=tmp_path)
    assert exc.value.retryable is False
    with pytest.raises(telegram.TelegramError, match="deploy_key"):
        telegram.make_transport(via_ssh=True, host="example.com", user="deploy", secrets_dir=tmp_path)
