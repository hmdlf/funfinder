"""
### archive — прошедшее из рабочих таблиц в схему `archive`

Раз в сутки. Правила — в `funfinder/ops/archive.py`.

1. **archive_past** — события старше 60 дней вместе с упоминаниями, посты без
   живых упоминаний, их записи в журнале извлечения, показы с сайтов и адреса
   страниц событий переносит в `archive.*`. Одна транзакция и та же блокировка
   каталога, что у `dedupe_events`: склейка и архив не мешают друг другу.
   Последний пост каждого источника остаётся: по нему считается водяной знак.
2. **trim_ingest_log** — удаляет из `meta.ingest_log` строки старше 90 дней.

Порог в 60 дней больше окна извлечения (30 дней) и срока страницы «событие
прошло» (30 дней), поэтому `rebuild_catalog` и `reextract` архив не трогают.
Если компьютер ночью выключен, запуск пройдёт при включении.
"""

import logging
from contextlib import closing
from datetime import timedelta

import pendulum
from airflow.sdk import Connection, dag, task

from funfinder.db import connect
from funfinder.ops import archive as archiving

log = logging.getLogger(__name__)

CONN_ID = "funfinder_db"


def _connect():
    # Как в ingest_vk: реквизиты из подключения Airflow, драйвер — psycopg2.
    return connect(Connection.get(CONN_ID).get_uri())


@dag(
    dag_id="archive",
    schedule="40 4 * * *",  # ночью, между загрузками
    start_date=pendulum.datetime(2026, 11, 1, tz="Europe/Moscow"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "funfinder",
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
    },
    tags=["ops", "archive"],
    doc_md=__doc__,
)
def archive():
    @task
    def archive_past() -> dict:
        with closing(_connect()) as conn:
            return archiving.run(conn)

    @task
    def trim_ingest_log() -> dict:
        with closing(_connect()) as conn:
            return archiving.trim_ingest_log(conn)

    archive_past()
    trim_ingest_log()


archive()
