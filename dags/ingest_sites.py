"""
### ingest_sites — афиши сайтов площадок в `raw.site_events`

Сайт театра — источник точнее пабликов: репертуар с датой и временем каждого
показа. Разбор у каждого сайта свой (`funfinder/sites/`), LLM не нужна.
Страницы, закрытые в robots.txt сайта, не загружаются.

1. **sync_sources** — переносит список источников из `sources/*.yaml` в `meta.sources`.
2. **list_sources** — активные сайты из базы.
3. **fetch_site** — dynamic task mapping: по задаче на сайт. Новые и
   изменённые показы записываются, пропавшие из афиши помечаются снятыми.
4. **emit_asset** — если что-то изменилось, публикует Asset `raw_site_events`:
   по нему запускается `process_events`.
5. **watcher** — помечает запуск упавшим, если упала любая задача.
"""

import logging
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pendulum
from airflow.sdk import Connection, TriggerRule, dag, get_current_context, task
from airflow.sdk.exceptions import AirflowFailException, AirflowSkipException

from funfinder import ingest_log, sources
from funfinder.assets import RAW_SITE_EVENTS
from funfinder.db import connect
from funfinder.sites.base import RobotsDisallowed, SiteFetcher, SiteFormatError
from funfinder.sites.ingest import ingest_site

log = logging.getLogger(__name__)

CONN_ID = "funfinder_db"


def _connect():
    return connect(Connection.get(CONN_ID).get_uri())


@dag(
    dag_id="ingest_sites",
    # Театры меняют афишу редко: дважды в день хватает.
    schedule="20 7,19 * * *",
    start_date=pendulum.datetime(2026, 11, 1, tz="Europe/Moscow"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "funfinder",
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
        "retry_exponential_backoff": True,
    },
    tags=["ingest", "sites"],
    doc_md=__doc__,
)
def ingest_sites():
    @task(retries=0)
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
                SELECT s.source_id, s.handle, c.timezone
                  FROM meta.sources s JOIN meta.cities c USING (city_id)
                 WHERE s.platform = 'site' AND s.is_active
                 ORDER BY s.source_id
                """
            )
            result = [{"source_id": sid, "handle": h, "timezone": tz} for sid, h, tz in cur.fetchall()]
        log.info("Активных сайтов: %d", len(result))
        return result

    @task(map_index_template="{{ source_handle }}")
    def fetch_site(source: dict) -> dict:
        ctx = get_current_context()
        ctx["source_handle"] = source["handle"]
        run = {"dag_id": ctx["dag"].dag_id, "run_id": ctx["run_id"], "source_id": source["source_id"]}
        today = datetime.now(ZoneInfo(source["timezone"])).date()

        with closing(_connect()) as conn:
            ingest_log.log_start(conn, attempt=ctx["ti"].try_number, **run)
            fetcher = SiteFetcher()
            try:
                result = ingest_site(fetcher, conn, source, run_id=ctx["run_id"], today=today)
            except (SiteFormatError, RobotsDisallowed) as exc:
                # Сайт переделали или закрыли страницу в robots.txt — повтор не поможет, нужна правка кода.
                ingest_log.log_finish(conn, status="failed", error=str(exc), **run)
                raise AirflowFailException(str(exc)) from exc
            except Exception as exc:
                ingest_log.log_finish(conn, status="failed", error=f"{type(exc).__name__}: {exc}", **run)
                raise
            finally:
                fetcher.close()
            stats = result.as_dict()
            # В журнале нет отдельного столбца для снятых показов: снятие — тоже изменение.
            ingest_log.log_finish(
                conn, status="success", stats={**stats, "updated": stats["updated"] + stats["removed"]}, **run
            )
        return stats

    @task(trigger_rule=TriggerRule.ALL_DONE, outlets=[RAW_SITE_EVENTS])
    def emit_asset() -> dict:
        ctx = get_current_context()
        with closing(_connect()) as conn:
            totals = ingest_log.run_totals(conn, dag_id=ctx["dag"].dag_id, run_id=ctx["run_id"])
        log.info("Итоги запуска: %s", totals)
        if totals["inserted"] + totals["updated"] == 0:
            raise AirflowSkipException("Афиши не изменились — Asset не публикуем")
        ctx["outlet_events"][RAW_SITE_EVENTS].extra = totals
        return totals

    @task(trigger_rule=TriggerRule.ONE_FAILED, retries=0)
    def watcher() -> None:
        raise AirflowFailException("Часть задач упала. Подробности — в логах fetch_site и в meta.ingest_log.")

    synced = sync_sources()
    listed = list_sources()
    fetched = fetch_site.expand(source=listed)
    emitted = emit_asset()

    synced >> listed
    fetched >> emitted
    [synced, listed, fetched, emitted] >> watcher()


ingest_sites()
