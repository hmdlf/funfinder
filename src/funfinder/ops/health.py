"""source_health: какие источники сломались или замолчали.

Каждому активному источнику — четыре вопроса.

1. **Загружается ли он?** Последние загрузки в meta.ingest_log с ошибкой
   подряд: одна-две — warn (сеть, разовый сбой), FAIL_AFTER и больше — fail.
   У каждой загрузки и так есть ретраи, поэтому три упавших запуска подряд —
   это уже не случайность.

   **Запускается ли загрузка вообще?** Строку в журнал пишет задача загрузки
   конкретного источника. Если DAG на паузе, запуск завис или упал раньше
   (sync_sources), строк нет совсем, и ошибок «подряд» тоже нет. Поэтому
   последняя попытка источника сравнивается с самой свежей успешной загрузкой
   любого источника: она показывает, что Airflow в это время работал.
   Отставание больше LAG_LIMIT для платформы — warn, вдвое больше — fail.
   Если компьютер был выключен, отстают все одинаково, тревоги нет. Загрузки
   последних LAG_GRACE часов в сравнение не берутся: при включении все
   пропущенные запуски стартуют разом, и кто-то неизбежно оказывается последним.

2. **Пишет ли он?** Только VK и Telegram. Молчание — время от последнего
   поста до последней успешной загрузки. Оно сравнивается с тем, сколько этот
   источник молчит обычно: 90-й процентиль промежутков между его постами за
   BASELINE_DAYS дней, умноженный на SILENCE_FACTOR, в границах
   MIN_SILENCE..MAX_SILENCE. У ежедневной афиши порог — 3 дня, у библиотеки,
   которая пишет раз в неделю, — около трёх недель. Дольше порога — warn,
   вдвое дольше — fail: разбор мог сломаться без ошибки (t.me поменял разметку,
   и постов «нет»), или паблик умер, и его пора убрать из yaml.
   Молчание меряется на момент последней успешной загрузки, а не «сейчас»:
   выключенный на выходные компьютер не делает источник замолчавшим.

3. **Даёт ли он события?** VK и Telegram: за 30 дней в LLM ушло
   POSTS_WITHOUT_EVENTS постов или больше, а событий — ни одного → warn
   (паблик перестал публиковать анонсы, или модель отказывается читать его
   посты). Сайт: в каталоге нет ни одного будущего показа → warn. Не fail:
   летом у театров перерыв.

Итог — meta.source_health, строка на (день, источник): повторная проверка в
тот же день переписывает строку. Правила — чистые функции без базы
(silence_limit, evaluate): их проверяют тесты без Postgres.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

FAIL_AFTER = 3  # загрузок с ошибкой подряд
BASELINE_DAYS = 90  # за сколько дней смотреть, как часто источник пишет
SILENCE_FACTOR = 3
MIN_SILENCE = timedelta(days=3)  # даже ежедневной афише можно взять выходные
MAX_SILENCE = timedelta(days=30)
DEFAULT_SILENCE = timedelta(days=14)  # постов слишком мало, чтобы судить о привычках
MIN_GAPS = 5  # столько промежутков между постами нужно, чтобы считать процентиль
YIELD_DAYS = 30  # то же окно, что у extract_events
POSTS_WITHOUT_EVENTS = 10
# Отставание загрузки от остальных: три пропущенных запуска по расписанию платформы.
LAG_LIMIT = {"vk": timedelta(hours=9), "tg": timedelta(hours=9), "site": timedelta(hours=36)}
LAG_GRACE = timedelta(hours=2)

POST_PLATFORMS = ("vk", "tg")


@dataclass
class Facts:
    """Всё, что известно об источнике из базы. Собирает collect()."""

    source_id: int
    platform: str
    handle: str
    timezone: str
    label: str | None = None  # note из yaml или название из VK: «Культ-клуб «Атмосфера»»
    runs: int = 0  # завершённых загрузок в журнале
    failures_in_row: int = 0
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None  # начало последней загрузки, с любым исходом
    reference_at: datetime | None = None  # последняя успешная загрузка любого источника старше LAG_GRACE
    last_error: str | None = None
    last_item_at: datetime | None = None
    item_times: list[datetime] = field(default_factory=list)  # публикации постов за BASELINE_DAYS
    posts_30d: int | None = None
    mentions_30d: int | None = None
    upcoming: int = 0


@dataclass
class Health:
    source_id: int
    platform: str
    handle: str
    checked_on: date  # день проверки по часовому поясу города
    status: str
    problems: list[str]
    facts: Facts
    silence_limit: timedelta | None = None

    @property
    def name(self) -> str:
        return f"{self.platform}/{self.handle}"

    @property
    def display(self) -> str:
        """vk/club221125269 (Культ-клуб «Атмосфера» / Липецк): по одному handle паблик не узнать."""
        return f"{self.name} ({self.facts.label})" if self.facts.label else self.name


def silence_limit(item_times: list[datetime]) -> timedelta:
    """Сколько источнику можно молчать: SILENCE_FACTOR × 90-й процентиль промежутков между постами."""
    times = sorted(item_times)
    gaps = sorted(b - a for a, b in zip(times, times[1:], strict=False))
    if len(gaps) < MIN_GAPS:
        return DEFAULT_SILENCE
    p90 = gaps[math.ceil(0.9 * len(gaps)) - 1]  # nearest-rank: без интерполяции, на малых выборках понятнее
    return min(max(p90 * SILENCE_FACTOR, MIN_SILENCE), MAX_SILENCE)


def fmt_span(span: timedelta) -> str:
    """«9 дн.» или «18 ч» — сокращения, чтобы не склонять числительные."""
    if span >= timedelta(days=2):
        return f"{span.days} дн."
    return f"{int(span.total_seconds() // 3600)} ч"


def evaluate(facts: Facts, now: datetime) -> Health:
    fails: list[str] = []
    warns: list[str] = []
    limit = None

    # 1. Загружается ли.
    error_lines = (facts.last_error or "").strip().splitlines()
    error = error_lines[0][:200] if error_lines else "текст ошибки не записан"
    if facts.runs == 0:
        warns.append("загрузок ещё не было")
    elif facts.failures_in_row >= FAIL_AFTER:
        fails.append(f"загрузок с ошибкой подряд: {facts.failures_in_row}. Последняя: {error}")
    elif facts.failures_in_row > 0:
        warns.append(f"загрузок с ошибкой подряд: {facts.failures_in_row}. Последняя: {error}")

    lag_limit = LAG_LIMIT.get(facts.platform)
    if lag_limit and facts.last_attempt_at and facts.reference_at:
        lag = facts.reference_at - facts.last_attempt_at
        text = f"загрузка не запускалась {fmt_span(lag)}, хотя другие источники за это время грузились"
        if lag > 2 * lag_limit:
            fails.append(text)
        elif lag > lag_limit:
            warns.append(text)

    if facts.platform in POST_PLATFORMS:
        # 2. Пишет ли.
        if facts.last_success_at is not None:
            if facts.last_item_at is None:
                warns.append("постов в базе нет")
            else:
                limit = silence_limit(facts.item_times)
                silence = facts.last_success_at - facts.last_item_at
                text = f"нет новых постов {fmt_span(silence)}, обычно — не дольше {fmt_span(limit)}"
                if silence > 2 * limit:
                    fails.append(text)
                elif silence > limit:
                    warns.append(text)
        # 3. Даёт ли события.
        if (facts.posts_30d or 0) >= POSTS_WITHOUT_EVENTS and not facts.mentions_30d:
            warns.append(f"постов за {YIELD_DAYS} дней: {facts.posts_30d}, событий в них: 0")
    elif facts.platform == "site" and facts.runs and facts.upcoming == 0:
        warns.append("в каталоге нет ни одного будущего показа")

    status = "fail" if fails else "warn" if warns else "ok"
    return Health(
        source_id=facts.source_id,
        platform=facts.platform,
        handle=facts.handle,
        checked_on=now.astimezone(ZoneInfo(facts.timezone)).date(),
        status=status,
        problems=fails + warns,
        facts=facts,
        silence_limit=limit,
    )


# --- База ---

SELECT_SOURCES = """
SELECT s.source_id, s.platform, s.handle, c.timezone, coalesce(nullif(s.note, ''), s.title)
  FROM meta.sources s
  JOIN meta.cities c USING (city_id)
 WHERE s.is_active
 ORDER BY s.platform, s.handle
"""

# Запуски, которые сейчас идут ('running'), в ошибки и успехи не считаем: исход ещё
# неизвестен. Но как попытка они считаются: зависший запуск со временем станет отставанием.
SELECT_RUNS = """
WITH last_ok AS (
    SELECT source_id, max(started_at) AS started_at, max(finished_at) AS finished_at
      FROM meta.ingest_log
     WHERE status = 'success'
     GROUP BY source_id
)
SELECT l.source_id,
       count(*) FILTER (WHERE l.status <> 'running'),
       count(*) FILTER (WHERE l.status = 'failed' AND l.started_at > coalesce(o.started_at, '-infinity')),
       o.finished_at,
       (array_agg(l.error ORDER BY l.started_at DESC) FILTER (WHERE l.status = 'failed'))[1],
       max(l.started_at)
  FROM meta.ingest_log l
  LEFT JOIN last_ok o USING (source_id)
 GROUP BY l.source_id, o.finished_at
"""

# Когда Airflow точно работал: последняя успешная загрузка любого активного источника,
# закончившаяся раньше, чем LAG_GRACE назад.
SELECT_REFERENCE = """
SELECT max(l.finished_at)
  FROM meta.ingest_log l
  JOIN meta.sources s USING (source_id)
 WHERE s.is_active AND l.status = 'success' AND l.finished_at <= %(before)s
"""

# Последний пост всегда в raw: archive его не уносит (по нему считается водяной знак).
SELECT_LAST_ITEM = """
SELECT source_id, max(published_at) FROM raw.posts GROUP BY source_id
UNION ALL
SELECT source_id, max(loaded_at) FROM raw.site_events GROUP BY source_id
"""

# Посты старше 60 дней уже в архиве, а привычки источника смотрим за 90.
SELECT_ITEM_TIMES = """
SELECT source_id, published_at FROM raw.posts WHERE published_at >= %(since)s
UNION ALL
SELECT source_id, published_at FROM archive.vk_posts WHERE published_at >= %(since)s
UNION ALL
SELECT source_id, published_at FROM archive.tg_posts WHERE published_at >= %(since)s
"""

# Посты, отправленные в LLM (пустые — без текста — не в счёт), и события из них, прошедшие проверку.
SELECT_YIELD = """
SELECT p.source_id,
       count(*),
       coalesce(sum(m.n), 0)
  FROM raw.posts p
  JOIN staging.post_extractions x USING (source_id, post_id)
  LEFT JOIN LATERAL (
        SELECT count(*) AS n
          FROM staging.events s
         WHERE s.source_id = p.source_id AND s.post_id = p.post_id
           AND s.validated AND s.quarantine_reason IS NULL
       ) m ON true
 WHERE p.published_at >= %(since)s
   AND x.status <> 'skipped_empty'
 GROUP BY p.source_id
"""

SELECT_UPCOMING = """
SELECT s.source_id, count(DISTINCT s.event_id)
  FROM staging.events s
  JOIN catalog.events e ON e.event_id = s.event_id
  JOIN meta.cities c ON c.city_id = e.city_id
 WHERE e.event_date >= (%(now)s AT TIME ZONE c.timezone)::date
 GROUP BY s.source_id
"""

UPSERT_HEALTH = """
INSERT INTO meta.source_health
       (checked_on, source_id, status, problems, failures_in_row, last_success_at, last_error,
        last_item_at, silence_limit, posts_30d, mentions_30d, upcoming, checked_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (checked_on, source_id) DO UPDATE SET
       status          = EXCLUDED.status,
       problems        = EXCLUDED.problems,
       failures_in_row = EXCLUDED.failures_in_row,
       last_success_at = EXCLUDED.last_success_at,
       last_error      = EXCLUDED.last_error,
       last_item_at    = EXCLUDED.last_item_at,
       silence_limit   = EXCLUDED.silence_limit,
       posts_30d       = EXCLUDED.posts_30d,
       mentions_30d    = EXCLUDED.mentions_30d,
       upcoming        = EXCLUDED.upcoming,
       checked_at      = EXCLUDED.checked_at
"""


def collect(conn, now: datetime) -> list[Facts]:
    with conn, conn.cursor() as cur:
        cur.execute(SELECT_SOURCES)
        facts = {row[0]: Facts(*row) for row in cur.fetchall()}

        cur.execute(SELECT_RUNS)
        for sid, runs, failures, last_success_at, last_error, last_attempt_at in cur.fetchall():
            if sid in facts:
                f = facts[sid]
                f.runs, f.failures_in_row, f.last_success_at = runs, failures, last_success_at
                f.last_error = last_error if failures else None
                f.last_attempt_at = last_attempt_at

        cur.execute(SELECT_REFERENCE, {"before": now - LAG_GRACE})
        reference_at = cur.fetchone()[0]
        for f in facts.values():
            f.reference_at = reference_at

        cur.execute(SELECT_LAST_ITEM)
        for sid, last_item_at in cur.fetchall():
            if sid in facts:
                facts[sid].last_item_at = last_item_at

        cur.execute(SELECT_ITEM_TIMES, {"since": now - timedelta(days=BASELINE_DAYS)})
        for sid, published_at in cur.fetchall():
            if sid in facts:
                facts[sid].item_times.append(published_at)

        for f in facts.values():
            if f.platform in POST_PLATFORMS:
                f.posts_30d = f.mentions_30d = 0
        cur.execute(SELECT_YIELD, {"since": now - timedelta(days=YIELD_DAYS)})
        for sid, posts, mentions in cur.fetchall():
            if sid in facts:
                facts[sid].posts_30d, facts[sid].mentions_30d = posts, int(mentions)

        cur.execute(SELECT_UPCOMING, {"now": now})
        for sid, upcoming in cur.fetchall():
            if sid in facts:
                facts[sid].upcoming = upcoming
    return list(facts.values())


def save(conn, results: list[Health], now: datetime) -> None:
    with conn, conn.cursor() as cur:
        for h in results:
            f = h.facts
            cur.execute(
                UPSERT_HEALTH,
                (
                    h.checked_on,
                    h.source_id,
                    h.status,
                    h.problems,
                    f.failures_in_row,
                    f.last_success_at,
                    f.last_error,
                    f.last_item_at,
                    h.silence_limit,
                    f.posts_30d,
                    f.mentions_30d,
                    f.upcoming,
                    now,
                ),
            )


def run(conn, *, now: datetime | None = None) -> list[Health]:
    now = now or datetime.now(UTC)
    results = [evaluate(f, now) for f in collect(conn, now)]
    save(conn, results, now)
    return results


def report(results: list[Health]) -> str:
    """Таблица для лога задачи: сначала сломанные, потом подозрительные."""
    order = {"fail": 0, "warn": 1, "ok": 2}
    width = max((len(h.name) for h in results), default=0)
    lines = []
    for h in sorted(results, key=lambda h: (order[h.status], h.name)):
        f = h.facts
        detail = "; ".join(h.problems) or (
            f"в каталоге: {f.upcoming}" + (f", постов за {YIELD_DAYS} дней: {f.posts_30d}" if f.posts_30d else "")
        )
        label = f"{f.label} — " if f.label else ""
        lines.append(f"{h.status:<4}  {h.name:<{width}}  {label}{detail}")
    return "\n".join(lines)


def summary(results: list[Health]) -> dict:
    out = {"ok": 0, "warn": 0, "fail": 0, "failed": [], "warned": []}
    for h in results:
        out[h.status] += 1
        if h.status == "fail":
            out["failed"].append(f"{h.display}: {'; '.join(h.problems)}")
        elif h.status == "warn":
            out["warned"].append(f"{h.display}: {'; '.join(h.problems)}")
    return out
