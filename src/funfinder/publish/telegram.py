"""Telegram Bot API: отправка сообщения в канал. Напрямую или через свой сервер.

Из России api.telegram.org закрыт так же, как t.me. Поэтому, как и загрузка
каналов (funfinder.tg.fetch), отправка умеет идти через сервер сайта в Европе:
по SSH на сервере запускается короткий скрипт на python3 из стандартной
библиотеки, он делает запрос и печатает ответ.

Токен бота — секрет, поэтому он уходит на сервер через stdin SSH-канала, а не
аргументом команды: аргументы видны в списке процессов сервера. По той же
причине токен вычищается из текста любых ошибок.

Ошибки двух видов (TelegramError.retryable):
- сеть, 429 (слишком часто), 5xx — повтор поможет;
- 400 (чат не найден, ошибка в разметке), 401 (неверный токен), 403 (бот не
  админ канала) — повтор не поможет, нужна правка настроек.
"""

import json
import logging
import shlex
from pathlib import Path

import paramiko
import requests

from funfinder.publish.errors import SendError
from funfinder.site import deploy

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"
TIMEOUT_S = 30


class TelegramError(SendError):
    pass


def _parse(status: int, body: str) -> dict:
    """Ответ Bot API → result. Ошибка — TelegramError с понятным текстом."""
    try:
        data = json.loads(body)
    except ValueError:
        raise TelegramError(f"HTTP {status}: ответ не JSON: {body[:200]!r}", retryable=status >= 500) from None
    if data.get("ok"):
        return data["result"]
    code = data.get("error_code", status)
    text = f"Telegram {code}: {data.get('description', 'без описания')}"
    retry_after = (data.get("parameters") or {}).get("retry_after")
    if retry_after:
        text += f" (повторить через {retry_after} с)"
    raise TelegramError(text, retryable=code == 429 or code >= 500)


class DirectTransport:
    """Прямой запрос: там, где api.telegram.org открывается."""

    def __init__(self, session: requests.Session | None = None, base_url: str = API_URL):
        self.session = session or requests.Session()
        self.base_url = base_url

    def call(self, token: str, method: str, params: dict) -> tuple[int, str]:
        try:
            resp = self.session.post(f"{self.base_url}/bot{token}/{method}", json=params, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            # Только тип ошибки: в её тексте был бы URL, а в нём токен.
            raise TelegramError(f"сеть: {type(exc).__name__}", retryable=True) from None
        return resp.status_code, resp.text

    def close(self) -> None:
        self.session.close()


# Выполняется на сервере: python3 без сторонних библиотек. Запрос — JSON из stdin,
# ответ — JSON в stdout: {"status": код, "body": текст} или {"error": что случилось}.
REMOTE_SCRIPT = """
import json, sys, urllib.error, urllib.request
req = json.load(sys.stdin)
url = req["base"] + "/bot" + req["token"] + "/" + req["method"]
data = json.dumps(req["params"]).encode()
r = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(r, timeout=TIMEOUT) as resp:
        out = {"status": resp.status, "body": resp.read().decode("utf-8", "replace")}
except urllib.error.HTTPError as e:
    out = {"status": e.code, "body": e.read().decode("utf-8", "replace")}
except Exception as e:
    out = {"error": type(e).__name__ + ": " + str(getattr(e, "reason", e))}
sys.stdout.write(json.dumps(out))
""".strip().replace("TIMEOUT", str(TIMEOUT_S))


def remote_command() -> str:
    return f"python3 -c {shlex.quote(REMOTE_SCRIPT)}"


class SSHTransport:
    """Запрос через свой сервер: скрипт на сервере, токен и текст — через stdin."""

    def __init__(self, client, base_url: str = API_URL):
        self.client = client  # paramiko.SSHClient, уже подключённый
        self.base_url = base_url

    @classmethod
    def connect(cls, host: str, **kwargs) -> "SSHTransport":
        """kwargs — как у funfinder.site.deploy.connect: user, key_path, known_hosts_path, port."""
        return cls(deploy.connect(host, **kwargs))

    def call(self, token: str, method: str, params: dict) -> tuple[int, str]:
        stdin, stdout, stderr = self.client.exec_command(remote_command(), timeout=TIMEOUT_S + 15)
        stdin.write(json.dumps({"base": self.base_url, "token": token, "method": method, "params": params}))
        stdin.channel.shutdown_write()
        raw = stdout.read()
        code = stdout.channel.recv_exit_status()
        if code != 0:
            raise TelegramError(
                f"скрипт на сервере завершился с кодом {code}: {stderr.read().decode()[-300:]}", retryable=True
            )
        out = json.loads(raw)
        if "error" in out:
            raise TelegramError(f"сеть на сервере: {out['error']}", retryable=True)
        return out["status"], out["body"]

    def close(self) -> None:
        self.client.close()


def make_transport(*, via_ssh: bool, host: str | None, user: str, secrets_dir: Path):
    """Транспорт по настройкам .env: напрямую или через сервер сайта (TG_FETCH_VIA_SSH).

    Ошибки настройки (нет хоста, ключа, чужой ключ сервера) — TelegramError без
    повтора: их чинит человек. Сетевые ошибки SSH пробрасываются как есть.
    """
    if not via_ssh:
        return DirectTransport()
    if not host:
        raise TelegramError(
            "TG_FETCH_VIA_SSH=true, но не задан SITE_DEPLOY_HOST — некуда подключаться", retryable=False
        )
    key, known_hosts = secrets_dir / "deploy_key", secrets_dir / "known_hosts"
    for path in (key, known_hosts):
        if not path.exists():
            raise TelegramError(f"Нет файла secrets/{path.name} — см. README, «Сайт: сервер»", retryable=False)
    try:
        return SSHTransport.connect(host, user=user, key_path=key, known_hosts_path=known_hosts)
    except paramiko.BadHostKeyException as exc:
        raise TelegramError(f"Ключ сервера не совпадает с secrets/known_hosts: {exc}", retryable=False) from exc
    except paramiko.AuthenticationException as exc:
        raise TelegramError(f"Сервер не принял ключ secrets/deploy_key: {exc}", retryable=False) from exc


class Bot:
    def __init__(self, token: str, transport):
        if not token:
            raise TelegramError("не задан токен бота (TG_BOT_TOKEN в .env)", retryable=False)
        self.token = token
        self.transport = transport

    def _call(self, method: str, params: dict) -> dict:
        try:
            status, body = self.transport.call(self.token, method, params)
            return _parse(status, body)
        except TelegramError as exc:
            # Текст ошибки уходит в лог и в meta.publish_log: токена в нём быть не должно.
            raise TelegramError(str(exc).replace(self.token, "***"), retryable=exc.retryable) from None

    def send(self, chat: str, html_text: str, *, key: str | None = None, link: str | None = None) -> str:
        """Отправляет HTML-сообщение без превью ссылок. Возвращает id сообщения.

        key и link — для общего интерфейса отправителей: у Bot API нет защиты от
        дублей по ключу, а ссылка на афишу уже есть в подвале текста.
        """
        result = self._call(
            "sendMessage",
            {
                "chat_id": chat,
                "text": html_text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
        )
        return str(result["message_id"])

    def chats(self) -> list[dict]:
        """Кто недавно писал боту или добавил его в канал: [{id, type, name}], без повторов.

        getUpdates хранит обновления сутки. Бот подборки сам обновления не читает,
        поэтому «/start» в личке бота виден здесь, пока не истёк этот срок.
        """
        found: dict[int, dict] = {}
        for update in self._call("getUpdates", {"limit": 100, "timeout": 0}):
            for key in ("message", "edited_message", "channel_post", "my_chat_member"):
                chat = (update.get(key) or {}).get("chat")
                if chat:
                    name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                    if chat.get("username"):
                        name = f"{name} (@{chat['username']})".strip()
                    found[chat["id"]] = {"id": chat["id"], "type": chat.get("type"), "name": name}
        return list(found.values())

    def close(self) -> None:
        self.transport.close()
