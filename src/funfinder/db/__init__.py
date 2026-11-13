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
