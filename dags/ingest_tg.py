"""
### ingest_tg — посты публичных каналов Telegram в `raw.tg_posts`

Читает веб-версию канала `t.me/s/<канал>`: без аккаунта, ключей и API.
Из России t.me не открывается, поэтому при `TG_FETCH_VIA_SSH=true` в `.env`
страницы загружаются через сервер сайта: по SSH выполняется `curl` на
сервере, HTML возвращается обратно. Ключ и `known_hosts` — те же, что
у выкладки сайта.

1. **sync_sources** — переносит список источников из `sources/*.yaml` в `meta.sources`.
2. **list_sources** — активные Telegram-каналы из базы.
3. **fetch_channel** — dynamic task mapping: по задаче на канал. Инкремент
   по id поста плюс перепроверка последней недели на правки. Идемпотентно.
4. **emit_asset** — если что-то добавилось или изменилось, публикует
   Asset `raw_tg_posts`: по нему запускается `process_events`.
5. **watcher** — помечает запуск упавшим, если упала любая задача.

Подробности по каждому каналу — в таблице `meta.ingest_log`.
"""

import logging
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import paramiko
import pendulum
from airflow.sdk import Connection, TriggerRule, Variable, dag, get_current_context, task
from airflow.sdk.exceptions import AirflowFailException, AirflowSkipException

from funfinder import ingest_log, sources
from funfinder.assets import RAW_TG_POSTS
from funfinder.db import connect
from funfinder.tg.fetch import DirectFetcher, SSHFetcher
from funfinder.tg.ingest import TGSettings, ingest_channel
from funfinder.tg.page import ChannelUnavailable, PageFormatError

log = logging.getLogger(__name__)

CONN_ID = "funfinder_db"
SECRETS_DIR = Path("/opt/airflow/secrets")  # ./secrets в проекте, только чтение


def _connect():
    # Как в ingest_vk: реквизиты из подключения Airflow, драйвер — psycopg2.
    return connect(Connection.get(CONN_ID).get_uri())


def _fetcher():
    """Напрямую или через сервер сайта — по переменной TG_FETCH_VIA_SSH из .env."""
    if Variable.get("tg_fetch_via_ssh", default="false").strip().lower() not in ("1", "true", "yes"):
        return DirectFetcher()
    host = Variable.get("site_deploy_host", default=None)
    if not host:
        raise AirflowFailException("TG_FETCH_VIA_SSH=true, но не задан SITE_DEPLOY_HOST — некуда подключаться")
    key, known_hosts = SECRETS_DIR / "deploy_key", SECRETS_DIR / "known_hosts"
    for path in (key, known_hosts):
        if not path.exists():
            raise AirflowFailException(f"Нет файла secrets/{path.name} — см. README, «Сайт: сервер»")
    try:
        return SSHFetcher.connect(
            host,
            user=Variable.get("site_deploy_user", default="deploy"),
            key_path=key,
            known_hosts_path=known_hosts,
        )
    except paramiko.BadHostKeyException as exc:
        raise AirflowFailException(f"Ключ сервера не совпадает с secrets/known_hosts: {exc}") from exc
    except paramiko.AuthenticationException as exc:
        raise AirflowFailException(f"Сервер не принял ключ secrets/deploy_key: {exc}") from exc


@dag(
    dag_id="ingest_tg",
    # Каждые 3 часа, на полчаса позже ingest_vk: два запуска process_events не встанут в очередь друг за другом.
    schedule="45 */3 * * *",
    start_date=pendulum.datetime(2026, 11, 1, tz="Europe/Moscow"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "funfinder",
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
        "retry_exponential_backoff": True,
    },
    tags=["ingest", "tg"],
    doc_md=__doc__,
)
def ingest_tg():
    # Ретраи нужны и здесь. Ошибка в yaml и так падает сразу (AirflowFailException), а повтор
    # спасает от сбоя запуска: 14.11 после включения компьютера задача не стартовала вовсе
    # («executor reported ... failed, but the task instance's state attribute is queued»),
    # и без ретраев пропал весь утренний запуск ingest_vk.
    @task
    def sync_sources() -> list[dict]:
        try:
            configs = sources.load_all()
        except sources.SourcesConfigError as exc:
            raise AirflowFailException(str(exc)) from exc
        with closing(_connect()) as conn:
            return [sources.sync_city(conn, cfg) for cfg in configs]

    @task
    def list_sources() -> list[dict]:
        with closing(_connect()) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.source_id, s.handle, s.settings
                  FROM meta.sources s
                 WHERE s.platform = 'tg' AND s.is_active
                 ORDER BY s.source_id
                """
            )
            result = [{"source_id": sid, "handle": handle, "settings": st} for sid, handle, st in cur.fetchall()]
        log.info("Активных Telegram-каналов: %d", len(result))
        return result

    @task(
        max_active_tis_per_dagrun=1,  # по одному каналу: t.me не любит частые запросы
        map_index_template="{{ source_handle }}",
    )
    def fetch_channel(source: dict) -> dict:
        ctx = get_current_context()
        ctx["source_handle"] = source["handle"]
        run = {"dag_id": ctx["dag"].dag_id, "run_id": ctx["run_id"], "source_id": source["source_id"]}
        try:
            settings = TGSettings.from_overrides(source["settings"])
        except ValueError as exc:
            raise AirflowFailException(f"{source['handle']}: {exc}") from exc

        with closing(_connect()) as conn:
            ingest_log.log_start(conn, attempt=ctx["ti"].try_number, **run)
            fetcher = None
            try:
                fetcher = _fetcher()
                result = ingest_channel(fetcher, conn, source, run_id=ctx["run_id"], settings=settings)
            except (ChannelUnavailable, PageFormatError, AirflowFailException) as exc:
                # Закрытый канал или изменившаяся разметка t.me — повтор не поможет.
                ingest_log.log_finish(conn, status="failed", error=str(exc), **run)
                raise AirflowFailException(str(exc)) from exc
            except Exception as exc:
                # Сеть, таймаут, сервер-посредник недоступен — Airflow повторит задачу.
                ingest_log.log_finish(conn, status="failed", error=f"{type(exc).__name__}: {exc}", **run)
                raise
            finally:
                if fetcher is not None:
                    fetcher.close()
            ingest_log.log_finish(conn, status="success", stats=result.as_dict(), **run)
        return result.as_dict()

    @task(trigger_rule=TriggerRule.ALL_DONE, outlets=[RAW_TG_POSTS])
    def emit_asset() -> dict:
        ctx = get_current_context()
        with closing(_connect()) as conn:
            totals = ingest_log.run_totals(conn, dag_id=ctx["dag"].dag_id, run_id=ctx["run_id"])
        log.info("Итоги запуска: %s", totals)
        if totals["inserted"] + totals["updated"] == 0:
            raise AirflowSkipException("Новых и изменённых постов нет — Asset не публикуем")
        ctx["outlet_events"][RAW_TG_POSTS].extra = totals
        return totals

    @task(trigger_rule=TriggerRule.ONE_FAILED, retries=0)
    def watcher() -> None:
        raise AirflowFailException(
            "Часть задач упала: смотрите задачи этого запуска в статусе failed. "
            "Ошибки загрузки по источникам — ещё и в логах fetch_channel и в meta.ingest_log."
        )

    synced = sync_sources()
    listed = list_sources()
    fetched = fetch_channel.expand(source=listed)
    emitted = emit_asset()

    synced >> listed
    fetched >> emitted
    [synced, listed, fetched, emitted] >> watcher()


ingest_tg()
