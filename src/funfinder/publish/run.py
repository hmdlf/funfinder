"""Публикация подборки в одно место: выбор дня, защита от дублей, журнал meta.publish_log.

День подборки. DAG запускается вечером и публикует подборку на завтра:
день = дата запуска по расписанию (по часовому поясу города) плюс один. Если
компьютер был выключен и пропущенный запуск выполнился уже после полуночи,
«завтра» наступило — такая подборка опоздала и не публикуется (late).
В ручном запуске день можно указать явно: тогда проверки на опоздание нет.

Дубли. Строка журнала на (место, день):
- sent — уже опубликовано, повторный запуск ничего не делает;
- sending — отправка началась, а чем кончилась, неизвестно (процесс оборвался
  посреди запроса). Сообщение могло уйти, а дубль в канале хуже пропуска,
  поэтому автоматически не повторяем — PublishError(retryable=False);
- failed, skipped — можно публиковать заново.
force=True публикует ещё раз в любом случае: для тестового канала.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from funfinder.publish import digest
from funfinder.publish.errors import SendError

log = logging.getLogger(__name__)

# Платформа → (формат текста, лимит длины).
FORMATS = {"tg": ("html", digest.TG_LIMIT), "vk": ("vk", digest.VK_LIMIT)}


class PublishError(Exception):
    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Plan:
    issue_date: date
    today: date  # сегодня по часовому поясу города: от него зависит «Завтра» или «Сегодня»
    late: bool


def plan(run_after: datetime, now: datetime, tz: str, override: date | None = None) -> Plan:
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    if override:
        return Plan(override, today, late=False)
    issue = run_after.astimezone(zone).date() + timedelta(days=1)
    return Plan(issue, today, late=today >= issue)


SELECT_LOG = "SELECT status, message_id FROM meta.publish_log WHERE target_id = %s AND issue_date = %s"

UPSERT_LOG = """
INSERT INTO meta.publish_log (target_id, issue_date, status, events, message_id, text, error, run_id, attempt, sent_at)
VALUES (%(target_id)s, %(issue_date)s, %(status)s, %(events)s, %(message_id)s, %(text)s, %(error)s,
        %(run_id)s, %(attempt)s, CASE WHEN %(status)s = 'sent' THEN now() END)
ON CONFLICT (target_id, issue_date) DO UPDATE SET
       status     = EXCLUDED.status,
       events     = coalesce(EXCLUDED.events, meta.publish_log.events),
       message_id = EXCLUDED.message_id,
       text       = coalesce(EXCLUDED.text, meta.publish_log.text),
       error      = EXCLUDED.error,
       run_id     = EXCLUDED.run_id,
       attempt    = EXCLUDED.attempt,
       sent_at    = EXCLUDED.sent_at,
       updated_at = now()
"""


def _log(conn, target_id: int, issue_date: date, status: str, *, run_id: str, attempt: int, **fields) -> None:
    row = {"events": None, "message_id": None, "text": None, "error": None} | fields
    with conn, conn.cursor() as cur:
        cur.execute(
            UPSERT_LOG,
            {
                "target_id": target_id,
                "issue_date": issue_date,
                "status": status,
                "run_id": run_id,
                "attempt": attempt,
                **row,
            },
        )


def publish(
    conn,
    target: dict,
    sender,
    *,
    issue: Plan,
    site_url: str,
    run_id: str,
    attempt: int = 1,
    force: bool = False,
) -> dict:
    """target — строка meta.publish_targets с городом (city_id, city_slug, city_name).

    sender — объект с методом send(handle, text, *, key, link) -> id сообщения, ошибки — SendError.
    key — ключ выпуска для защиты от дублей на стороне площадки (guid в VK), link —
    адрес афиши города (карточка под записью VK). Ничего не знает об Airflow.
    """
    tid, day = target["target_id"], issue.issue_date
    with conn, conn.cursor() as cur:
        cur.execute(SELECT_LOG, (tid, day))
        previous = cur.fetchone()
    if previous and not force:
        status, message_id = previous
        if status == "sent":
            return {"status": "already_sent", "issue_date": day.isoformat(), "message_id": message_id}
        if status == "sending":
            raise PublishError(
                f"Прошлая отправка подборки на {day} оборвалась, неизвестно, ушло ли сообщение. "
                "Проверьте канал. Если сообщения нет — запустите publish вручную с force.",
                retryable=False,
            )

    settings = digest.DigestSettings.from_overrides(target.get("settings"))
    city = {"city_id": target["city_id"], "slug": target["city_slug"], "name": target["city_name"]}
    d = digest.load(conn, city, day, settings, site_url=site_url, today=issue.today, platform=target["platform"])
    run = {"run_id": run_id, "attempt": attempt}
    if not d.count:
        _log(conn, tid, day, "skipped", events=0, error="на этот день событий нет", **run)
        return {"status": "skipped", "issue_date": day.isoformat(), "events": 0}

    platform = target["platform"]
    fmt, limit = FORMATS[platform]
    if target.get("draft_chat"):
        limit = digest.VK_DRAFT_LIMIT  # текст для VK уйдёт сообщением Telegram
    text, shown = digest.render(d, today=issue.today, fmt=fmt, limit=limit)
    _log(conn, tid, day, "sending", events=shown, text=text, **run)
    try:
        message_id = sender.send(target["handle"], text, key=f"{tid}:{day}:{run_id}", link=d.city_url)
    except SendError as exc:
        _log(conn, tid, day, "failed", events=shown, error=str(exc), **run)
        raise PublishError(str(exc), retryable=exc.retryable) from exc
    _log(conn, tid, day, "sent", events=shown, message_id=message_id, **run)
    log.info(
        "%s %s: подборка на %s, событий %d из %d, сообщение %s",
        platform,
        target["handle"],
        day,
        shown,
        d.count,
        message_id,
    )
    return {
        "status": "sent",
        "issue_date": day.isoformat(),
        "events": shown,
        "total": d.count,
        "message_id": message_id,
    }
