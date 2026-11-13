"""Применяет SQL-миграции из папки migrations/ по порядку имён.

Применённые версии хранятся в meta.schema_migrations вместе с контрольной
суммой. Уже применённый файл менять нельзя: мигратор это заметит и
остановится. Правка схемы — всегда новый файл с бо́льшим номером.

Запуск: python -m funfinder.db.migrate
"""

import hashlib
import logging
import sys
from pathlib import Path

from funfinder.db import connect

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# Один мигратор за раз, даже если контейнеры стартуют одновременно.
ADVISORY_LOCK_ID = 0x46554E46  # 'FUNF'


class MigrationError(RuntimeError):
    pass


def _checksum(sql: str) -> str:
    # CRLF → LF: на Windows git может переписать окончания строк,
    # и та же миграция не должна считаться изменённой.
    return hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def migrate(conn, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Применяет новые миграции в одной транзакции. Возвращает их версии."""
    applied_now: list[str] = []
    with conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_ID,))
        cur.execute("CREATE SCHEMA IF NOT EXISTS meta")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS meta.schema_migrations (
                version    text PRIMARY KEY,
                checksum   text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute("SELECT version, checksum FROM meta.schema_migrations")
        applied = dict(cur.fetchall())

        for path in sorted(migrations_dir.glob("*.sql")):
            version = path.stem
            sql = path.read_text(encoding="utf-8")
            checksum = _checksum(sql)
            if version in applied:
                if applied[version] != checksum:
                    raise MigrationError(
                        f"Миграция {version} изменилась после применения. "
                        "Верните файл как был и добавьте новую миграцию."
                    )
                continue
            log.info("Применяю %s", version)
            cur.execute(sql)
            cur.execute(
                "INSERT INTO meta.schema_migrations (version, checksum) VALUES (%s, %s)",
                (version, checksum),
            )
            applied_now.append(version)
    return applied_now


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = connect()
    try:
        applied = migrate(conn)
    finally:
        conn.close()
    log.info("Применено миграций: %d %s", len(applied), applied or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
