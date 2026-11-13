"""
### ingest_vk — посты из публичных сообществ VK в `raw.vk_posts`

1. **sync_sources** — переносит список источников из `sources/*.yaml` в `meta.sources`.
2. **list_sources** — активные VK-источники из базы.
3. **fetch_source** — dynamic task mapping: по задаче на сообщество.
   Инкремент по id поста плюс перепроверка последних дней на правки.
   Идемпотентно: повторный запуск не создаёт дублей.
4. **emit_asset** — если что-то добавилось или изменилось, публикует
   Asset `raw_vk_posts`. Следующие DAG-и запускаются по нему, а не по часам.
   Если нового нет, задача пропускается и Asset не публикуется.
5. **watcher** — помечает весь запуск упавшим, если упала любая задача.
   Без него запуск выглядел бы успешным: успешные листовые задачи
   скрывают упавшие в середине графа.

Подробности каждого источника — в таблице `meta.ingest_log`.
"""

import logging
from contextlib import closing
from datetime import timedelta

import pendulum
from airflow.sdk import Connection, TriggerRule, Variable, dag, get_current_context, task
from airflow.sdk.exceptions import AirflowFailException, AirflowSkipException

from funfinder import ingest_log, sources
from funfinder.assets import RAW_VK_POSTS
from funfinder.db import connect
from funfinder.vk.client import VKAccessError, VKAuthError, VKClient
from funfinder.vk.ingest import IngestSettings, SourceUnavailable, ingest_source, resolve_source

log = logging.getLogger(__name__)

CONN_ID = "funfinder_db"


def _connect():
    # Реквизиты берём из подключения Airflow, а драйвер — явно psycopg2.
    # PostgresHook.get_conn() в Airflow 3.3 отдаёт psycopg 3, если он
    # установлен (а в образе он есть), и код проекта на нём бы сломался.
    return connect(Connection.get(CONN_ID).get_uri())


@dag(
    dag_id="ingest_vk",
    schedule="15 */3 * * *",  # каждые 3 часа, время московское (default_timezone)
    start_date=pendulum.datetime(2026, 11, 1, tz="Europe/Moscow"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "funfinder",
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
        "retry_exponential_backoff": True,
    },
    tags=["ingest", "vk"],
    doc_md=__doc__,
)
def ingest_vk():
    @task(retries=0)
    def sync_sources() -> list[dict]:
        try:
            configs = sources.load_all()
        except sources.SourcesConfigError as exc:
            # Ошибка в yaml: повтор не поможет, нужна правка файла.
            raise AirflowFailException(str(exc)) from exc
        with closing(_connect()) as conn:
            return [sources.sync_city(conn, cfg) for cfg in configs]

    @task
    def list_sources() -> list[dict]:
        with closing(_connect()) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.source_id, s.handle, s.external_id, s.settings, c.slug
                  FROM meta.sources s
                  JOIN meta.cities c USING (city_id)
                 WHERE s.platform = 'vk' AND s.is_active
                 ORDER BY s.source_id
                """
            )
            rows = cur.fetchall()
        result = [
            {"source_id": sid, "handle": handle, "external_id": ext, "settings": settings, "city": city}
            for sid, handle, ext, settings, city in rows
        ]
        log.info("Активных VK-источников: %d", len(result))
        return result

    @task(
        # Не больше двух сообществ одновременно: у VK API лимит на частоту запросов.
        max_active_tis_per_dagrun=2,
        # В UI у каждой копии задачи будет имя паблика, а не номер.
        map_index_template="{{ source_handle }}",
    )
    def fetch_source(source: dict) -> dict:
        ctx = get_current_context()
        ctx["source_handle"] = source["handle"]
        run = {"dag_id": ctx["dag"].dag_id, "run_id": ctx["run_id"], "source_id": source["source_id"]}

        token = Variable.get("vk_service_token", default=None)
        if not token:
            raise AirflowFailException("Не задан VK_SERVICE_TOKEN в .env (переменная Airflow vk_service_token)")
        client = VKClient(token)
        try:
            settings = IngestSettings.from_overrides(source["settings"])
        except ValueError as exc:
            raise AirflowFailException(f"{source['handle']}: {exc}") from exc

        with closing(_connect()) as conn:
            ingest_log.log_start(conn, attempt=ctx["ti"].try_number, **run)
            try:
                resolved = resolve_source(client, conn, source)
                result = ingest_source(client, conn, resolved, run_id=ctx["run_id"], settings=settings)
            except (VKAuthError, VKAccessError, SourceUnavailable) as exc:
                # Токен или доступ к сообществу — повтор не поможет.
                ingest_log.log_finish(conn, status="failed", error=str(exc), **run)
                raise AirflowFailException(str(exc)) from exc
            except Exception as exc:
                # Сеть, лимиты, 5xx — Airflow повторит задачу.
                ingest_log.log_finish(conn, status="failed", error=f"{type(exc).__name__}: {exc}", **run)
                raise
            ingest_log.log_finish(conn, status="success", stats=result.as_dict(), **run)
        return result.as_dict()

    @task(trigger_rule=TriggerRule.ALL_DONE, outlets=[RAW_VK_POSTS])
    def emit_asset() -> dict:
        # ALL_DONE: если часть пабликов упала, то, что загрузилось, всё равно
        # пойдёт дальше по пайплайну.
        ctx = get_current_context()
        with closing(_connect()) as conn:
            totals = ingest_log.run_totals(conn, dag_id=ctx["dag"].dag_id, run_id=ctx["run_id"])
        log.info("Итоги запуска: %s", totals)
        if totals["inserted"] + totals["updated"] == 0:
            raise AirflowSkipException("Новых и изменённых постов нет — Asset не публикуем")
        ctx["outlet_events"][RAW_VK_POSTS].extra = totals
        return totals

    @task(trigger_rule=TriggerRule.ONE_FAILED, retries=0)
    def watcher() -> None:
        raise AirflowFailException("Часть задач упала. Подробности — в логах fetch_source и в meta.ingest_log.")

    synced = sync_sources()
    listed = list_sources()
    fetched = fetch_source.expand(source=listed)
    emitted = emit_asset()

    synced >> listed
    fetched >> emitted
    [synced, listed, fetched, emitted] >> watcher()


ingest_vk()
