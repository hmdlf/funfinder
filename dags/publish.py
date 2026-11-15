"""
### publish — подборка «Завтра в <городе>» в каналы

Каждый вечер в 20:00 публикует подборку событий на завтра в каждое место из
раздела `publish:` файла `sources/<город>.yaml`. Логика — в `funfinder/publish/`.

1. **sync_targets** — переносит yaml в базу: места публикации — в
   `meta.publish_targets` (вместе с источниками и площадками, как `sync_sources`).
2. **list_targets** — активные места публикации.
3. **publish_digest** — dynamic task mapping: по задаче на место. Подборка
   на завтра из `catalog.events_public`: по категориям, со ссылками на страницы
   событий на сайте. Журнал — `meta.publish_log`: повторный запуск не публикует
   второй раз. Если компьютер был выключен и запуск выполнился уже после
   полуночи, подборка опоздала — задача пропускается.
4. **watcher** — помечает запуск упавшим, если упала любая задача.

Telegram: бот из `TG_BOT_TOKEN`; при `TG_FETCH_VIA_SSH=true` отправка идёт через
сервер сайта, как и загрузка каналов.

VK: право писать на стену через API VK новым приложениям не выдаёт (2026), поэтому
по умолчанию — полуавтомат: при `draft_to` в yaml бот присылает готовый текст для VK
в этот чат Telegram, публикует человек. Без `draft_to` — запись на стене через API с
ключом VK ID (`python -m funfinder.publish.vk_auth`), если доступ к стене когда-нибудь дадут.

**Параметры ручного запуска:**
- `issue_date` — на какой день подборка (по умолчанию — на завтра).
- `force` — опубликовать, даже если подборка на этот день уже вышла. Для тестового канала.
"""

import logging
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pendulum
from airflow.sdk import Connection, Param, TriggerRule, Variable, dag, get_current_context, task
from airflow.sdk.exceptions import AirflowFailException, AirflowSkipException

from funfinder import sources
from funfinder.db import connect
from funfinder.publish import run, vkid
from funfinder.publish.errors import SendError
from funfinder.publish.telegram import Bot, make_transport
from funfinder.publish.vk import Draft, Wall

log = logging.getLogger(__name__)

CONN_ID = "funfinder_db"
SECRETS_DIR = Path("/opt/airflow/secrets")  # ./secrets в проекте, только чтение

SELECT_TARGETS = """
SELECT t.target_id, t.platform, t.handle, t.note, t.settings, t.draft_chat,
       c.city_id, c.slug, c.name, c.timezone
  FROM meta.publish_targets t
  JOIN meta.cities c USING (city_id)
 WHERE t.is_active
 ORDER BY t.target_id
"""


def _connect():
    # Как в ingest_vk: реквизиты из подключения Airflow, драйвер — psycopg2.
    return connect(Connection.get(CONN_ID).get_uri())


def _telegram_transport():
    """Напрямую или через сервер сайта — по TG_FETCH_VIA_SSH, как у ingest_tg."""
    return make_transport(
        via_ssh=Variable.get("tg_fetch_via_ssh", default="false").strip().lower() in ("1", "true", "yes"),
        host=Variable.get("site_deploy_host", default=None),
        user=Variable.get("site_deploy_user", default="deploy"),
        secrets_dir=SECRETS_DIR,
    )


def _sender(target: dict):
    platform = target["platform"]
    try:
        if platform == "tg":
            return Bot(Variable.get("tg_bot_token", default=""), _telegram_transport())
        if platform == "vk" and target.get("draft_chat"):
            # Полуавтомат: текст для VK присылает бот в Telegram, публикует человек.
            return Draft(Bot(Variable.get("tg_bot_token", default=""), _telegram_transport()), target["draft_chat"])
        if platform == "vk":
            # Ключ VK ID берётся из базы при отправке: своё подключение, короткая транзакция.
            return Wall(vkid.TokenStore(_connect, vkid.secret_box()))
    except SendError as exc:
        raise AirflowFailException(str(exc)) from exc
    raise AirflowFailException(f"Публиковать в {platform} не умеем")


@dag(
    dag_id="publish",
    # Вечером, когда домашний компьютер обычно включён: после загрузок 18:15, 18:45 и 19:20.
    schedule="0 20 * * *",
    start_date=pendulum.datetime(2026, 11, 1, tz="Europe/Moscow"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "funfinder",
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
        "retry_exponential_backoff": True,
    },
    tags=["publish", "telegram", "vk"],
    doc_md=__doc__,
    params={
        "issue_date": Param(
            None,
            type=["null", "string"],
            format="date",
            title="День подборки",
            description="Пусто — на завтра. Дата — подборка на этот день, без проверки на опоздание.",
        ),
        "force": Param(
            False,
            type="boolean",
            title="Опубликовать ещё раз",
            description="Даже если подборка на этот день уже вышла. Для тестового канала.",
        ),
    },
)
def publish():
    @task
    def sync_targets() -> list[dict]:
        try:
            configs = sources.load_all()
        except sources.SourcesConfigError as exc:
            raise AirflowFailException(str(exc)) from exc  # ошибка в yaml: повтор не поможет
        with closing(_connect()) as conn:
            return [sources.sync_city(conn, cfg) for cfg in configs]

    @task
    def list_targets() -> list[dict]:
        with closing(_connect()) as conn, conn.cursor() as cur:
            cur.execute(SELECT_TARGETS)
            names = [
                "target_id",
                "platform",
                "handle",
                "note",
                "settings",
                "draft_chat",
                "city_id",
                "city_slug",
                "city_name",
                "timezone",
            ]
            targets = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
        log.info("Мест публикации: %d", len(targets))
        return targets

    @task(map_index_template="{{ target_name }}")
    def publish_digest(target: dict) -> dict:
        ctx = get_current_context()
        ctx["target_name"] = f"{target['platform']} {target['handle']}"
        params = ctx["params"]
        override = date.fromisoformat(params["issue_date"]) if params.get("issue_date") else None
        issue = run.plan(ctx["dag_run"].run_after, datetime.now(UTC), target["timezone"], override)
        if issue.late:
            raise AirflowSkipException(f"Подборка на {issue.issue_date} опоздала: этот день уже наступил")

        sender = _sender(target)
        try:
            with closing(_connect()) as conn:
                result = run.publish(
                    conn,
                    target,
                    sender,
                    issue=issue,
                    site_url=Variable.get("site_url", default="https://matingu.com"),
                    run_id=ctx["run_id"],
                    attempt=ctx["ti"].try_number,
                    force=bool(params.get("force")),
                )
        except run.PublishError as exc:
            if exc.retryable:
                raise  # сеть, 429, 5xx — Airflow повторит
            raise AirflowFailException(str(exc)) from exc
        finally:
            sender.close()

        log.info("Итог: %s", result)
        if result["status"] == "already_sent":
            raise AirflowSkipException(f"Подборка на {result['issue_date']} уже опубликована: {result['message_id']}")
        if result["status"] == "skipped":
            raise AirflowSkipException(f"На {result['issue_date']} событий нет — публиковать нечего")
        return result

    @task(trigger_rule=TriggerRule.ONE_FAILED, retries=0)
    def watcher() -> None:
        raise AirflowFailException(
            "Часть задач упала: смотрите задачи этого запуска в статусе failed. "
            "Что именно не ушло — в логах publish_digest и в meta.publish_log."
        )

    synced = sync_targets()
    listed = list_targets()
    published = publish_digest.expand(target=listed)

    synced >> listed
    [synced, listed, published] >> watcher()


publish()
