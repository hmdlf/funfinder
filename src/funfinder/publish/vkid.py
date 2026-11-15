"""VK ID: ключ пользователя, с которым DAG publish пишет на стену сообщества.

Почему ключ пользователя. Метод wall.post не работает ни с ключом сообщества,
ни с сервисным ключом приложения: VK требует ключ человека, который админит
сообщество. Такой ключ выдаёт VK ID по OAuth 2.1 — пользователь входит через
браузер и разрешает приложению доступ к стене. Право «Стена» VK ID даёт только
приложениям подтверждённых (верифицированных) владельцев.

Протокол — authorization code с PKCE:
1. start_login() — адрес входа со случайными state и code_challenge;
2. человек входит, VK ID переадресует браузер на redirect_uri приложения,
   в адресе — code, state и device_id;
3. exchange_code() — code + code_verifier → access_token (живёт час) и
   refresh_token (живёт полгода).
Шаги 1–3 делает команда python -m funfinder.publish.vk_auth.

Обновление. Каждый запуск publish берёт ключ через TokenStore.access_token():
если ключ ещё жив — отдаёт его, если истёк — обменивает refresh_token на
новую пару. Тонкости VK ID:
- старый refresh_token после обмена больше не действует, а повторная попытка
  обменять его же отзывает всю сессию. Поэтому новая пара сохраняется сразу,
  в той же транзакции, а обмен идёт под блокировкой строки (SELECT … FOR UPDATE):
  две задачи не обменяют один refresh_token дважды;
- если ответ на обмен потерялся в сети, VK мог уже выдать новую пару. Повтор
  задачи тогда упрётся в отозванную сессию — нужен новый вход (AuthRequired).

Токены в базе зашифрованы (Fernet). Ключ — тот же AIRFLOW_FERNET_KEY, которым
Airflow шифрует подключения: дамп базы без .env токенов не раскрывает.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, quote, urlencode, urlparse

import requests
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from funfinder.publish.errors import SendError

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://id.vk.ru/authorize"
TOKEN_URL = "https://id.vk.ru/oauth2/auth"
PROVIDER = "vk_id"
# wall — публикация на стене; groups — проверить, что вошедший админит сообщество.
SCOPES = ("wall", "groups")
TIMEOUT_S = 30
# Ключ, которому осталось жить меньше, обновляем заранее: публикация не должна
# застать его истекающим посреди запроса.
EXPIRY_MARGIN = timedelta(minutes=5)

LOGIN_COMMAND = "docker compose run --rm airflow-cli python -m funfinder.publish.vk_auth"


class VKIDError(SendError):
    """Ошибка VK ID. retryable — сеть и сбои VK; остальное — нужен человек."""


class AuthRequired(VKIDError):
    """Ключа нет или он больше не действует: нужен новый вход через браузер."""

    def __init__(self, reason: str):
        super().__init__(f"{reason}. Войдите заново: {LOGIN_COMMAND}", retryable=False)


# --- PKCE и адрес входа ---


def new_verifier() -> str:
    """code_verifier: 64 символа из [A-Za-z0-9_-] (PKCE разрешает 43–128)."""
    return secrets.token_urlsafe(48)


def code_challenge(verifier: str) -> str:
    """S256: base64url от SHA-256, без «=» на конце."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class Login:
    """Начатый вход: то, что нужно помнить до обмена кода."""

    client_id: str
    redirect_uri: str
    state: str
    verifier: str

    def url(self, authorize_url: str = AUTHORIZE_URL) -> str:
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "scope": " ".join(SCOPES),
            "redirect_uri": self.redirect_uri,
            "state": self.state,
            "code_challenge": code_challenge(self.verifier),
            "code_challenge_method": "S256",
        }
        return f"{authorize_url}?{urlencode(query, quote_via=quote)}"  # пробел в scope — %20, не «+»


def start_login(client_id: str, redirect_uri: str) -> Login:
    # VK ID принимает в state только [A-Za-z0-9_-] и не короче 32 символов.
    return Login(str(client_id), redirect_uri, state=secrets.token_urlsafe(32), verifier=new_verifier())


def parse_redirect(url: str) -> dict[str, str]:
    """Адрес, на который VK ID вернул браузер → code, state, device_id.

    Параметры приходят в query (?code=…&state=…&device_id=…). Старые виджеты
    присылали их JSON-ом в параметре payload — разбираем и так.
    """
    parsed = urlparse(url.strip())
    params = dict(parse_qsl(parsed.query)) | dict(parse_qsl(parsed.fragment))
    if "payload" in params:
        try:
            params |= {k: str(v) for k, v in json.loads(params.pop("payload")).items()}
        except (ValueError, AttributeError):
            raise ValueError("параметр payload в адресе — не JSON") from None
    if "error" in params:
        raise ValueError(f"VK ID отказал во входе: {params['error']}: {params.get('error_description', '')}".strip())
    missing = [k for k in ("code", "state", "device_id") if not params.get(k)]
    if missing:
        raise ValueError(f"в адресе нет {', '.join(missing)} — скопируйте адрес целиком, после входа")
    return {k: params[k] for k in ("code", "state", "device_id")}


# --- Запросы к VK ID ---


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: datetime
    user_id: int | None
    scope: str

    def __repr__(self) -> str:  # токены не должны попасть в лог даже случайно
        return f"Tokens(user_id={self.user_id}, scope={self.scope!r}, expires_at={self.expires_at.isoformat()})"


def _post(session: requests.Session, url: str, data: dict) -> dict:
    try:
        resp = session.post(url, data=data, timeout=TIMEOUT_S)
    except requests.RequestException as exc:
        raise VKIDError(f"VK ID: сеть: {type(exc).__name__}", retryable=True) from None
    try:
        body = resp.json()
    except ValueError:
        raise VKIDError(f"VK ID: HTTP {resp.status_code}, ответ не JSON", retryable=resp.status_code >= 500) from None
    if not isinstance(body, dict):
        raise VKIDError(f"VK ID: HTTP {resp.status_code}, неожиданный ответ", retryable=False)
    error = body.get("error")
    if error or resp.status_code >= 400:
        text = f"VK ID: {error or f'HTTP {resp.status_code}'}: {body.get('error_description', '')}".rstrip(": ")
        if resp.status_code >= 500 or error in ("server_error", "temporarily_unavailable", "slow_down"):
            raise VKIDError(text, retryable=True)
        # invalid_grant, invalid_request, invalid_client: ключ отозван, истёк или уже обменян.
        raise AuthRequired(text)
    return body


def _tokens(body: dict, now: datetime, *, sent_state: str, old_refresh: str | None = None) -> Tokens:
    if body.get("state") not in (None, sent_state):
        raise VKIDError("VK ID: state в ответе не совпал с отправленным — ответ не на наш запрос", retryable=False)
    refresh = body.get("refresh_token") or old_refresh
    if not body.get("access_token") or not refresh:
        raise VKIDError(f"VK ID: в ответе нет токенов, поля: {sorted(body)}", retryable=False)
    user_id = body.get("user_id")
    return Tokens(
        access_token=body["access_token"],
        refresh_token=refresh,
        expires_at=now + timedelta(seconds=int(body.get("expires_in") or 3600)),
        user_id=int(user_id) if user_id else None,
        scope=str(body.get("scope") or ""),
    )


def exchange_code(
    session: requests.Session, login: Login, code: str, device_id: str, *, now: datetime, token_url: str = TOKEN_URL
) -> Tokens:
    """Код из адреса после входа → первая пара токенов."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": login.verifier,
        "client_id": login.client_id,
        "device_id": device_id,
        "redirect_uri": login.redirect_uri,
        "state": login.state,
    }
    return _tokens(_post(session, token_url, data), now, sent_state=login.state)


def refresh(
    session: requests.Session,
    client_id: str,
    device_id: str,
    refresh_token: str,
    *,
    now: datetime,
    token_url: str = TOKEN_URL,
) -> Tokens:
    """refresh_token → новая пара. Старый refresh_token после этого не действует."""
    state = secrets.token_urlsafe(32)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "device_id": device_id,
        "state": state,
    }
    return _tokens(_post(session, token_url, data), now, sent_state=state, old_refresh=refresh_token)


# --- Хранение ---


def secret_box(keys: str | None = None) -> MultiFernet:
    """Шифр для токенов. keys — ключи Fernet через запятую, как AIRFLOW__CORE__FERNET_KEY:
    первый шифрует, любой расшифровывает (так Airflow меняет ключи без потери данных).
    """
    raw = keys if keys is not None else os.environ.get("AIRFLOW__CORE__FERNET_KEY", "")
    parts = [k.strip() for k in raw.split(",") if k.strip()]
    if not parts:
        raise VKIDError("нет ключа шифрования токенов: AIRFLOW_FERNET_KEY в .env", retryable=False)
    return MultiFernet([Fernet(k) for k in parts])


SELECT_FOR_UPDATE = """
SELECT client_id, device_id, access_token, refresh_token, access_expires_at
  FROM meta.oauth_tokens
 WHERE provider = %s
   FOR UPDATE
"""

UPSERT = """
INSERT INTO meta.oauth_tokens (provider, client_id, device_id, user_id, scope,
                               access_token, refresh_token, access_expires_at, authorized_at)
VALUES (%(provider)s, %(client_id)s, %(device_id)s, %(user_id)s, %(scope)s,
        %(access)s, %(refresh)s, %(expires)s, %(now)s)
ON CONFLICT (provider) DO UPDATE SET
       client_id         = EXCLUDED.client_id,
       device_id         = EXCLUDED.device_id,
       user_id           = EXCLUDED.user_id,
       scope             = EXCLUDED.scope,
       access_token      = EXCLUDED.access_token,
       refresh_token     = EXCLUDED.refresh_token,
       access_expires_at = EXCLUDED.access_expires_at,
       authorized_at     = EXCLUDED.authorized_at,
       refreshed_at      = NULL,
       updated_at        = now()
"""

UPDATE_REFRESHED = """
UPDATE meta.oauth_tokens
   SET access_token      = %(access)s,
       refresh_token     = %(refresh)s,
       access_expires_at = %(expires)s,
       user_id           = coalesce(%(user_id)s, user_id),
       scope             = coalesce(nullif(%(scope)s, ''), scope),
       refreshed_at      = %(now)s,
       updated_at        = now()
 WHERE provider = %(provider)s
"""

SELECT_INFO = """
SELECT client_id, user_id, scope, access_expires_at, authorized_at, refreshed_at
  FROM meta.oauth_tokens
 WHERE provider = %s
"""


class TokenStore:
    """Пара токенов VK ID в meta.oauth_tokens: сохранить после входа, выдать живой ключ.

    connect — функция без аргументов, открывающая новое подключение к базе:
    у хранилища свои короткие транзакции, независимые от транзакций публикации.
    """

    def __init__(
        self,
        connect: Callable,
        box: MultiFernet,
        session: requests.Session | None = None,
        *,
        provider: str = PROVIDER,
        token_url: str = TOKEN_URL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.connect = connect
        self.box = box
        self.session = session or requests.Session()
        self.provider = provider
        self.token_url = token_url
        self.clock = clock

    def _encrypt(self, value: str) -> str:
        return self.box.encrypt(value.encode()).decode("ascii")

    def _decrypt(self, value: str) -> str:
        try:
            return self.box.decrypt(value.encode("ascii")).decode()
        except InvalidToken:
            raise AuthRequired("токены VK ID не расшифровываются: сменился AIRFLOW_FERNET_KEY") from None

    def save(self, client_id: str, device_id: str, tokens: Tokens) -> None:
        """После входа через браузер: новая пара заменяет старую целиком."""
        conn = self.connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    UPSERT,
                    {
                        "provider": self.provider,
                        "client_id": str(client_id),
                        "device_id": device_id,
                        "user_id": tokens.user_id,
                        "scope": tokens.scope,
                        "access": self._encrypt(tokens.access_token),
                        "refresh": self._encrypt(tokens.refresh_token),
                        "expires": tokens.expires_at,
                        "now": self.clock(),
                    },
                )
        finally:
            conn.close()

    def access_token(self, *, force_refresh: bool = False) -> str:
        """Живой ключ: сохранённый или, если истёк, обновлённый.

        force_refresh — обновить, даже если по времени ключ жив: VK его не принял.
        """
        conn = self.connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(SELECT_FOR_UPDATE, (self.provider,))
                row = cur.fetchone()
                if row is None:
                    raise AuthRequired("вход в VK ID ещё не выполнен")
                client_id, device_id, access, refresh_enc, expires_at = row
                now = self.clock()
                if not force_refresh and expires_at - EXPIRY_MARGIN > now:
                    return self._decrypt(access)

                tokens = refresh(
                    self.session, client_id, device_id, self._decrypt(refresh_enc), now=now, token_url=self.token_url
                )
                # Старый refresh_token уже не действует: новая пара должна попасть в базу
                # в этой же транзакции, до любых других запросов.
                cur.execute(
                    UPDATE_REFRESHED,
                    {
                        "provider": self.provider,
                        "access": self._encrypt(tokens.access_token),
                        "refresh": self._encrypt(tokens.refresh_token),
                        "expires": tokens.expires_at,
                        "user_id": tokens.user_id,
                        "scope": tokens.scope,
                        "now": now,
                    },
                )
                log.info("VK ID: ключ обновлён, действует до %s", tokens.expires_at.isoformat())
                return tokens.access_token
        finally:
            conn.close()

    def info(self) -> dict | None:
        """Что сохранено — без самих токенов. None — входа не было."""
        conn = self.connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(SELECT_INFO, (self.provider,))
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        names = ("client_id", "user_id", "scope", "access_expires_at", "authorized_at", "refreshed_at")
        return dict(zip(names, row, strict=True))
