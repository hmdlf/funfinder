"""Загрузка страниц t.me: напрямую или через свой сервер по SSH.

Из России t.me не открывается без обходных средств. Поэтому есть второй
способ: подключиться по SSH к серверу сайта (он в Европе) и выполнить там
curl. HTML возвращается по тому же соединению. Ключ и known_hosts — те же,
что у выкладки сайта, отдельный прокси на сервере не нужен.
"""

import logging
import shlex

import requests

from funfinder.site import deploy

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; FunFinder/1.0; +https://matingu.com)"
TIMEOUT_S = 30


class FetchError(Exception):
    """Страница не загрузилась: сеть, таймаут, 5xx. Airflow повторит задачу."""


def page_url(channel: str, before: int | None = None) -> str:
    return f"https://t.me/s/{channel}" + (f"?before={int(before)}" if before else "")


class DirectFetcher:
    """Прямой запрос: там, где t.me открывается."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def get(self, url: str) -> str:
        try:
            resp = self.session.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise FetchError(f"{url}: {exc}") from exc
        return resp.text

    def close(self) -> None:
        self.session.close()


def curl_command(url: str) -> str:
    """Команда для сервера. URL собирается из проверенного имени канала, но кавычки — всё равно."""
    return (
        f"curl --silent --show-error --fail --location --compressed --max-time {TIMEOUT_S} "
        f"--user-agent {shlex.quote(USER_AGENT)} {shlex.quote(url)}"
    )


class SSHFetcher:
    """Запрос через свой сервер: curl на сервере, HTML — обратно по SSH."""

    def __init__(self, client):
        self.client = client  # paramiko.SSHClient, уже подключённый

    @classmethod
    def connect(cls, host: str, **kwargs) -> "SSHFetcher":
        """kwargs — как у funfinder.site.deploy.connect: user, key_path, known_hosts_path, port."""
        return cls(deploy.connect(host, **kwargs))

    def get(self, url: str) -> str:
        _, stdout, stderr = self.client.exec_command(curl_command(url), timeout=TIMEOUT_S + 15)
        body = stdout.read()  # сначала читаем вывод: иначе на большой странице канал SSH встанет
        status = stdout.channel.recv_exit_status()
        if status != 0:
            raise FetchError(f"{url}: curl на сервере завершился с кодом {status}: {stderr.read().decode().strip()}")
        return body.decode("utf-8")

    def close(self) -> None:
        self.client.close()
