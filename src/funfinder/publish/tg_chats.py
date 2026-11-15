"""Id чатов, которые недавно писали боту подборки: для draft_to в sources/<город>.yaml.

    docker compose run --rm airflow-cli python -m funfinder.publish.tg_chats

Сначала напишите боту в Telegram что угодно (например, /start), потом запустите
команду: она покажет id вашего чата с ботом. Telegram хранит такие сообщения
для бота сутки.

Бот и путь до Telegram — те же, что у DAG publish: TG_BOT_TOKEN и, при
TG_FETCH_VIA_SSH=true, сервер сайта (SITE_DEPLOY_HOST, secrets/). В контейнере
они доступны как переменные Airflow AIRFLOW_VAR_*.
"""

import os
import sys
from collections.abc import Callable
from pathlib import Path

from funfinder.publish.errors import SendError
from funfinder.publish.telegram import Bot, make_transport

SECRETS_DIR = Path(os.environ.get("FUNFINDER_SECRETS_DIR", "/opt/airflow/secrets"))
TYPES = {"private": "личный чат", "channel": "канал", "group": "группа", "supergroup": "группа"}


def show(bot, out: Callable[[str], None] = print) -> int:
    chats = bot.chats()
    if not chats:
        out("Боту пока никто не писал. Напишите ему в Telegram /start и запустите команду снова.")
        return 1
    out("Недавно писали боту:")
    for chat in chats:
        out(f"  {chat['id']:>16}  {TYPES.get(chat['type'], chat['type'])}: {chat['name'] or '—'}")
    out("\nВаш id — в строке «личный чат». Впишите его в sources/<город>.yaml у сообщества VK: draft_to: <id>")
    return 0


def main() -> int:
    env = os.environ.get
    try:
        transport = make_transport(
            via_ssh=env("AIRFLOW_VAR_TG_FETCH_VIA_SSH", "false").strip().lower() in ("1", "true", "yes"),
            host=env("AIRFLOW_VAR_SITE_DEPLOY_HOST") or None,
            user=env("AIRFLOW_VAR_SITE_DEPLOY_USER") or "deploy",
            secrets_dir=SECRETS_DIR,
        )
        bot = Bot(env("AIRFLOW_VAR_TG_BOT_TOKEN", ""), transport)
        try:
            return show(bot)
        finally:
            bot.close()
    except SendError as exc:
        print(f"Ошибка: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
