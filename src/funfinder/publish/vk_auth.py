"""Вход в VK ID для публикации подборки в сообщество VK. Запускается руками, один раз.

    docker compose run --rm airflow-cli python -m funfinder.publish.vk_auth          # вход
    docker compose run --rm airflow-cli python -m funfinder.publish.vk_auth status   # проверка

Вход: команда печатает адрес, его нужно открыть в браузере, где вы вошли в VK
как администратор сообщества, и разрешить доступ. VK ID переадресует браузер
на VK_ID_REDIRECT_URI — страница может не открыться, это нормально: нужен
только адрес из адресной строки. Его вставляете в команду, она обменивает код
на ключи, сохраняет их в meta.oauth_tokens (зашифрованными) и проверяет
сообщества VK из раздела publish: в sources/<город>.yaml.

Повторный вход нужен, только если ключи перестали действовать: refresh_token
не использовался полгода, вход отозвали в настройках VK или сменился
AIRFLOW_FERNET_KEY. DAG publish в этом случае падает с текстом «Войдите заново».

Настройки — из .env: VK_ID_CLIENT_ID (ID приложения VK ID) и VK_ID_REDIRECT_URI
(доверенный адрес из настроек приложения).
"""

import argparse
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime

import requests

from funfinder import sources
from funfinder.db import connect
from funfinder.publish import vkid
from funfinder.publish.errors import SendError
from funfinder.publish.vk import Wall

DEFAULT_REDIRECT_URI = "http://localhost/vk-auth"


def vk_groups() -> list[tuple[str, str]]:
    """Сообщества VK из раздела publish: всех городов — (город, адрес)."""
    return [(cfg.slug, t.handle) for cfg in sources.load_all() for t in cfg.publish if t.platform == "vk"]


def check_groups(store, groups: list[tuple[str, str]], out: Callable[[str], None], *, wall_factory=Wall) -> bool:
    """Может ли вошедший публиковать в каждое сообщество. True — во все."""
    if not groups:
        out("В sources/<город>.yaml нет сообществ VK. Добавьте в раздел publish:")
        out("  - platform: vk\n    group: https://vk.com/club123456   # адрес сообщества\n    note: Тестовое")
        return True
    wall, ok = wall_factory(store), True
    for city, handle in groups:
        try:
            group = wall.group(handle)
        except SendError as exc:
            out(f"  ✗ {city}: {handle} — {exc}")
            ok = False
        else:
            name = f"{group.get('name', handle)} (id {group['id']})"
            if "is_admin" in group:
                out(f"  ✓ {city}: {name} — можно публиковать")
            else:
                out(f"  ? {city}: {name} — VK не сообщил, админ ли вы; проверится при первой публикации")
    return ok


def login(
    store: vkid.TokenStore,
    client_id: str,
    redirect_uri: str,
    *,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    session: requests.Session | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> vkid.Tokens:
    started = vkid.start_login(client_id, redirect_uri)
    out("1. Откройте адрес в браузере, где вы вошли в VK как администратор сообщества:\n")
    out(started.url())
    out("\n2. Разрешите доступ. Браузер перейдёт на " + redirect_uri)
    out("   Страница может не открыться — это нормально. Скопируйте адрес из адресной строки целиком.\n")
    params = vkid.parse_redirect(ask("3. Вставьте адрес сюда и нажмите Enter: "))
    if params["state"] != started.state:
        raise ValueError("state в адресе не тот: адрес от другого входа. Запустите команду заново")

    tokens = vkid.exchange_code(session or store.session, started, params["code"], params["device_id"], now=now())
    store.save(client_id, params["device_id"], tokens)
    out(f"\nГотово: ключи сохранены. Пользователь VK id {tokens.user_id}, права: {tokens.scope or '—'}")
    if "wall" not in tokens.scope.split():
        out(
            "ВНИМАНИЕ: VK не выдал право wall — публиковать на стене не получится. Проверьте вкладку «Доступы»"
            " приложения в кабинете VK ID: «Стена» должна быть включена."
        )
    return tokens


def status(store: vkid.TokenStore, out: Callable[[str], None] = print) -> bool:
    info = store.info()
    if info is None:
        out("Вход в VK ID не выполнен. Запустите: " + vkid.LOGIN_COMMAND)
        return False
    out(f"Приложение VK ID: {info['client_id']}, пользователь VK id {info['user_id']}")
    out(f"Права: {info['scope'] or '—'}")
    out(f"Вход через браузер: {info['authorized_at']:%Y-%m-%d %H:%M %Z}")
    refreshed = info["refreshed_at"]
    out(f"Последнее обновление ключа: {f'{refreshed:%Y-%m-%d %H:%M %Z}' if refreshed else 'ещё не было'}")
    out(f"Текущий ключ действует до: {info['access_expires_at']:%Y-%m-%d %H:%M %Z}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m funfinder.publish.vk_auth", description=__doc__.split("\n")[0])
    parser.add_argument("command", nargs="?", choices=["login", "status"], default="login")
    args = parser.parse_args(argv)

    store = vkid.TokenStore(connect, vkid.secret_box())
    try:
        if args.command == "status":
            if not status(store):
                return 1
            print("\nСообщества VK из sources/:")
            return 0 if check_groups(store, vk_groups(), print) else 1

        client_id = os.environ.get("VK_ID_CLIENT_ID", "").strip()
        if not client_id:
            print("Не задан VK_ID_CLIENT_ID в .env: ID приложения из кабинета VK ID (id.vk.com → Мои приложения)")
            return 2
        redirect_uri = os.environ.get("VK_ID_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI
        login(store, client_id, redirect_uri)
        print("\nСообщества VK из sources/:")
        return 0 if check_groups(store, vk_groups(), print) else 1
    except (ValueError, SendError, sources.SourcesConfigError) as exc:
        print(f"\nОшибка: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
