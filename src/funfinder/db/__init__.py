import os

import psycopg2


def dsn_from_env() -> str:
    """Строка подключения к базе проекта.

    FUNFINDER_DB_DSN — для запуска вне Airflow (тесты, ноутбук).
    AIRFLOW_CONN_FUNFINDER_DB — та же строка, которую видит Airflow.
    """
    dsn = os.environ.get("FUNFINDER_DB_DSN") or os.environ.get("AIRFLOW_CONN_FUNFINDER_DB")
    if not dsn:
        raise RuntimeError("Не задана строка подключения: FUNFINDER_DB_DSN или AIRFLOW_CONN_FUNFINDER_DB")
    return dsn


def connect(dsn: str | None = None):
    return psycopg2.connect(dsn or dsn_from_env())


# Каталог меняют две задачи из разных DAG-ов: dedupe_events (process_events)
# и archive_past (archive). Блокировка на транзакцию ставит их в очередь:
# склейка не привяжет упоминание к событию, которое архив в этот момент уносит.
CATALOG_LOCK_ID = 0x46554E43  # 'FUNC'


def lock_catalog(cur) -> None:
    """Ждёт, пока каталог не меняет другая транзакция. Снимается сама в конце транзакции."""
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (CATALOG_LOCK_ID,))
