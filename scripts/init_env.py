"""Создаёт .env из .env.example: генерирует пароли баз и секреты Airflow.

Запуск из корня репозитория: python scripts/init_env.py
Существующий .env не перезаписывается. Нужен только стандартный Python.
"""

import base64
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    env, example = ROOT / ".env", ROOT / ".env.example"
    if env.exists():
        print(".env уже есть, не трогаю. Удалите его, если хотите создать заново.")
        return 1

    generated = {
        # Пароли — hex: без символов, которые пришлось бы кодировать в строке подключения.
        "POSTGRES_PASSWORD": secrets.token_hex(16),
        "FUNFINDER_DB_PASSWORD": secrets.token_hex(16),
        # Ключ Fernet — это 32 случайных байта в urlsafe base64.
        "AIRFLOW_FERNET_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "AIRFLOW_JWT_SECRET": secrets.token_urlsafe(32),
    }
    if sys.platform.startswith("linux"):
        generated["AIRFLOW_UID"] = str(os.getuid())

    lines = []
    for line in example.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0]
        lines.append(f"{key}={generated[key]}" if key in generated and not line.startswith("#") else line)
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("Создан .env: сгенерированы пароли баз и секреты Airflow.")
    print("Осталось вписать VK_SERVICE_TOKEN, YANDEX_API_KEY и YANDEX_FOLDER_ID (см. README).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
