"""
### source_health — какие источники сломались или замолчали

Раз в сутки проверяет каждый активный источник по журналу загрузок
`meta.ingest_log` и по данным. Правила — в `funfinder/ops/health.py`.

1. **check_sources** — четыре вопроса к каждому источнику: загружается ли он
   (ошибки подряд), запускается ли загрузка вообще (отставание от остальных
   источников: DAG на паузе или упал до загрузки), пишет ли он (молчание
   дольше обычного для этого паблика) и даёт ли он события. Итог — в `meta.source_health`, строка на день и
   источник; последняя проверка — `meta.source_health_latest`. В логе задачи —
   таблица: сначала сломанные, потом подозрительные.
2. **alert** — красная, если хоть один источник в статусе `fail`. Статус `warn`
   задачу не роняет: он виден в таблице и в логе `check_sources`.
   Уведомления (Telegram, почта) подключаются здесь же, когда понадобятся.

Если компьютер был выключен, проверка запустится при включении: молчание
считается на момент последней успешной загрузки, поэтому ложных тревог это
не даёт.
"""

import logging
from contextlib import closing
from datetime import timedelta

import pendulum
from airflow.sdk import Connection, dag, task
from airflow.sdk.exceptions import AirflowFailException

from funfinder.db import connect
from funfinder.ops import health

log = logging.getLogger(__name__)

CONN_ID = "funfinder_db"


def _connect():
    # Как в ingest_vk: реквизиты из подключения Airflow, драйвер — psycopg2.
    return connect(Connection.get(CONN_ID).get_uri())


@dag(
    dag_id="source_health",
    # Вечером, после загрузок 18:15 (VK), 18:45 (Telegram) и 19:20 (сайты).
    schedule="50 20 * * *",
    start_date=pendulum.datetime(2026, 11, 1, tz="Europe/Moscow"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "funfinder",
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["ops", "monitoring"],
    doc_md=__doc__,
)
def source_health():
    @task
    def check_sources() -> dict:
        with closing(_connect()) as conn:
            results = health.run(conn)
        log.info("Источники:\n%s", health.report(results))
        return health.summary(results)

    @task  # сломанный источник — AirflowFailException, без повторов; ретраи — от сбоев запуска
    def alert(summary: dict) -> None:
        for line in summary["warned"]:
            log.warning("Подозрительно: %s", line)
        if summary["failed"]:
            raise AirflowFailException("Сломаны источники:\n" + "\n".join(summary["failed"]))
        log.info("Сломанных источников нет: ok %d, warn %d", summary["ok"], summary["warn"])

    alert(check_sources())


source_health()
