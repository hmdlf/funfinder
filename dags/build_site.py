"""
### build_site — сайт-афиша из каталога событий

Запускается сам, когда `process_events` публикует Asset `catalog_events`.

1. **render_site** — собирает статические страницы из `catalog.events_public`
   в `site_build/site/`: страницу города, страницы категорий, sitemap,
   robots, 404. Сначала во временную папку, потом подменяет: упавшая сборка
   не портит предыдущую. Посмотреть локально:
   `python -m http.server -d site_build/site 8000` → http://localhost:8000
2. **deploy_site** — заливает собранное на сервер по SSH в новую папку
   `releases/<время>/` и атомарно переключает на неё ссылку `current`.
   Если сервер не настроен (`SITE_DEPLOY_HOST` пуст), задача пропускается:
   сайт собран только локально.
"""

import logging
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import paramiko
import pendulum
from airflow.sdk import Connection, Variable, dag, task
from airflow.sdk.exceptions import AirflowFailException, AirflowSkipException

from funfinder.assets import CATALOG_EVENTS
from funfinder.db import connect
from funfinder.site import build, deploy

log = logging.getLogger(__name__)

CONN_ID = "funfinder_db"
BUILD_DIR = Path("/opt/airflow/site_build")  # ./site_build в проекте
SECRETS_DIR = Path("/opt/airflow/secrets")  # ./secrets в проекте, только чтение


def _connect():
    # Как в ingest_vk: реквизиты из подключения Airflow, драйвер — psycopg2.
    return connect(Connection.get(CONN_ID).get_uri())


@dag(
    dag_id="build_site",
    schedule=[CATALOG_EVENTS],  # по событию: каталог изменился — пересобираем
    start_date=pendulum.datetime(2026, 11, 1, tz="Europe/Moscow"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "funfinder",
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
        "retry_exponential_backoff": True,
    },
    tags=["site"],
    doc_md=__doc__,
)
def build_site():
    @task
    def render_site() -> dict:
        site_url = Variable.get("site_url", default="https://matingu.com")
        contact = Variable.get("site_contact", default=None) or None
        with closing(_connect()) as conn:
            stats = build.run(conn, BUILD_DIR, site_url=site_url, contact=contact)
        log.info("Собрано: %s", stats)
        return stats

    @task
    def deploy_site() -> dict:
        host = Variable.get("site_deploy_host", default=None)
        if not host:
            raise AirflowSkipException("SITE_DEPLOY_HOST не задан — сайт собран только локально, в site_build/site")
        key, known_hosts = SECRETS_DIR / "deploy_key", SECRETS_DIR / "known_hosts"
        for path in (key, known_hosts):
            if not path.exists():
                raise AirflowFailException(f"Нет файла secrets/{path.name} — см. README, «Сайт: сервер»")
        try:
            return deploy.deploy(
                BUILD_DIR / "site",
                host=host,
                user=Variable.get("site_deploy_user", default="deploy"),
                key_path=key,
                known_hosts_path=known_hosts,
                remote_dir=Variable.get("site_deploy_dir", default="/var/www/matingu"),
            )
        except paramiko.BadHostKeyException as exc:
            # Ключ сервера не совпал с secrets/known_hosts: либо переустановили сервер,
            # либо подмена. Повтор не поможет, нужен человек.
            raise AirflowFailException(f"Ключ сервера не совпадает с secrets/known_hosts: {exc}") from exc
        except paramiko.AuthenticationException as exc:
            raise AirflowFailException(f"Сервер не принял ключ secrets/deploy_key: {exc}") from exc

    render_site() >> deploy_site()


build_site()
