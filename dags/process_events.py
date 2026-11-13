"""
### process_events — из сырых постов в каталог событий

Запускается сам, когда `ingest_vk` или `ingest_tg` публикует Asset
`raw_vk_posts` или `raw_tg_posts`.

1. **extract_events** — новые и изменённые посты обеих платформ за последние
   30 дней (вместе с текстом репоста VK или цитатой ответа в Telegram)
   отправляет в LLM. События пишет в
   `staging.events`, итог по посту — в журнал `staging.post_extractions`
   (статус, модель, токены). Одна транзакция на пост: ретрай продолжит
   с того места, где упал. Если модель не ответила за 2 минуты, пост
   получает статус `timeout` и перепрогоняется в следующем запуске,
   а остальные посты обрабатываются дальше.
2. **validate_quality** — проверяет новые строки `staging.events`: без
   названия, без даты, дата раньше публикации или больше чем на год вперёд →
   `quarantine_reason`. Категория не из `meta.categories` → `other`.
3. **resolve_venues** — привязывает текст места к площадке из справочника
   `sources/venues/<город>.yaml` («ОЦКиНТ» и «Центр культуры, народного
   творчества и кино» — одна площадка). У пабликов самих площадок
   «малый зал» и пустое место → площадка паблика. Что не нашлось —
   в `staging.unmatched_venues`.
4. **dedupe_events** — склеивает упоминания в `catalog.events`: тот же город
   и дата, совместимое время, похожее название. Разные площадки — разные
   события, одна площадка — порог похожести названий ниже. Сравнивает и с
   событиями прошлых запусков. Если каталог изменился, публикует Asset
   `catalog_events`.

Карантин — представление `quarantine.events`, витрина — `catalog.events_public`.

**Параметры ручного запуска** (по Asset запускается с выключенными):
- `rebuild_catalog` — заново привязать площадки и пересобрать каталог из
  `staging.events` с нуля. Бесплатно, без вызовов LLM. Нужно после смены
  правил склейки и после правки справочника площадок.
- `reextract` — заново прогнать через LLM посты, обработанные старой
  версией промпта. Стоит денег. Каталог при этом пересобирается сам.
"""

import logging
from contextlib import closing
from datetime import timedelta

import pendulum
from airflow.sdk import Connection, Param, Variable, dag, get_current_context, task
from airflow.sdk.exceptions import AirflowFailException, AirflowSkipException

from funfinder.assets import CATALOG_EVENTS, RAW_TG_POSTS, RAW_VK_POSTS
from funfinder.db import connect
from funfinder.events import dedupe, extract, validate, venues
from funfinder.events.llm import LLMClient, LLMConfigError

log = logging.getLogger(__name__)

CONN_ID = "funfinder_db"


def _connect():
    # Как в ingest_vk: реквизиты из подключения Airflow, драйвер — psycopg2.
    return connect(Connection.get(CONN_ID).get_uri())


@dag(
    dag_id="process_events",
    schedule=(RAW_VK_POSTS | RAW_TG_POSTS),  # по событию: любая платформа загрузила новые или изменённые посты
    start_date=pendulum.datetime(2026, 11, 1, tz="Europe/Moscow"),
    catchup=False,
    max_active_runs=1,  # два запуска одновременно склеивали бы одни и те же строки
    default_args={
        "owner": "funfinder",
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
        "retry_exponential_backoff": True,
    },
    tags=["events", "llm"],
    doc_md=__doc__,
    params={
        "rebuild_catalog": Param(
            False,
            type="boolean",
            title="Пересобрать каталог",
            description="Заново привязать площадки и склеить все упоминания. Бесплатно: LLM не вызывается.",
        ),
        "reextract": Param(
            False,
            type="boolean",
            title="Перепрогнать посты через LLM",
            description="Посты, обработанные старой версией промпта, — заново. Стоит денег.",
        ),
    },
)
def process_events():
    @task(execution_timeout=timedelta(hours=1))
    def extract_events() -> dict:
        reextract = get_current_context()["params"]["reextract"]
        api_key = Variable.get("yandex_api_key", default=None)
        folder_id = Variable.get("yandex_folder_id", default=None)
        if not api_key or not folder_id:
            raise AirflowFailException(
                "Не заданы YANDEX_API_KEY / YANDEX_FOLDER_ID в .env "
                "(переменные Airflow yandex_api_key, yandex_folder_id)"
            )
        llm = LLMClient(api_key, folder_id)
        with closing(_connect()) as conn:
            try:
                stats = extract.run(conn, llm, reextract=reextract)
            except LLMConfigError as exc:
                # Ключ, права, имя модели — повтор не поможет.
                raise AirflowFailException(str(exc)) from exc
        log.info("Итоги извлечения: %s", stats)
        return stats

    @task
    def validate_quality() -> dict:
        with closing(_connect()) as conn:
            stats = validate.run(conn)
        log.info("Итоги проверки: %s", stats)
        return stats

    @task
    def resolve_venues() -> dict:
        params = get_current_context()["params"]
        # После правки справочника старые упоминания тоже надо перепривязать — вместе с пересборкой.
        full = params["rebuild_catalog"] or params["reextract"]
        with closing(_connect()) as conn:
            stats = venues.run(conn, full=full)
        log.info("Итоги привязки площадок: %s", stats)
        return stats

    @task(outlets=[CATALOG_EVENTS])
    def dedupe_events() -> dict:
        ctx = get_current_context()
        # После перепрогона постов старые события склеены из уже удалённых упоминаний — тоже пересобираем.
        rebuild = ctx["params"]["rebuild_catalog"] or ctx["params"]["reextract"]
        with closing(_connect()) as conn:
            stats = dedupe.run(conn, rebuild=rebuild)
        log.info("Итоги склейки: %s", stats)
        if stats["created"] + stats["linked"] + stats["deleted"] == 0:
            raise AirflowSkipException("Каталог не изменился — Asset не публикуем")
        ctx["outlet_events"][CATALOG_EVENTS].extra = stats
        return stats

    extract_events() >> validate_quality() >> resolve_venues() >> dedupe_events()


process_events()
