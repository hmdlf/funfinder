"""source_health: правила без базы и сквозная проверка на настоящем Postgres."""

from datetime import UTC, date, datetime, timedelta

import pytest

from conftest import NOW, FakeLLM, ev, load, make_post, pipeline
from funfinder.ops import archive, health
from funfinder.ops.health import Facts, evaluate, silence_limit

H = timedelta(hours=1)
D = timedelta(days=1)


def every(step: timedelta, count: int, *, last: datetime = NOW) -> list[datetime]:
    return [last - step * i for i in range(count)]


def facts(platform="vk", **kw) -> Facts:
    """Здоровый паблик: грузится, пишет каждый день, события есть."""
    defaults = {
        "runs": 20,
        "last_success_at": NOW - H,
        "last_item_at": NOW - 5 * H,
        "item_times": every(D, 60, last=NOW - 5 * H),
        "posts_30d": 30,
        "mentions_30d": 25,
        "upcoming": 12,
    }
    if platform == "site":
        defaults.update(item_times=[], posts_30d=None, mentions_30d=None)
    return Facts(1, platform, "test", "Europe/Moscow", **{**defaults, **kw})


# --- Сколько можно молчать ---


@pytest.mark.parametrize(
    ("times", "expected"),
    [
        (every(6 * H, 100), 3 * D),  # пишет каждые 6 часов: 18 ч → не меньше трёх дней
        (every(D, 60), 3 * D),  # каждый день
        (every(7 * D, 12), 21 * D),  # раз в неделю: три недели
        (every(30 * D, 6), 30 * D),  # раз в месяц: 90 дней → не больше 30
        (every(D, 4), health.DEFAULT_SILENCE),  # три промежутка — мало, чтобы судить
        ([], health.DEFAULT_SILENCE),
    ],
)
def test_silence_limit(times, expected):
    assert silence_limit(times) == expected


def test_silence_limit_ignores_rare_long_gaps():
    # Каждый день, но дважды был перерыв на неделю: 90-й процентиль — всё ещё «день».
    times = every(D, 40) + [NOW - 46 * D, NOW - 53 * D]
    assert silence_limit(times) == 3 * D


# --- Правила ---


def test_healthy_source():
    h = evaluate(facts(), NOW)
    assert (h.status, h.problems) == ("ok", [])
    assert h.silence_limit == 3 * D


def test_failures_in_a_row():
    one = evaluate(facts(failures_in_row=1, last_error="FetchError: t.me timed out\nTraceback…"), NOW)
    assert one.status == "warn"
    assert one.problems == ["загрузок с ошибкой подряд: 1. Последняя: FetchError: t.me timed out"]
    three = evaluate(facts(failures_in_row=3, last_error="VKError 5: token expired"), NOW)
    assert three.status == "fail"
    assert "VKError 5" in three.problems[0]
    assert "не записан" in evaluate(facts(failures_in_row=3, last_error=None), NOW).problems[0]


def test_never_loaded():
    h = evaluate(facts(runs=0, last_success_at=None, last_item_at=None, item_times=[]), NOW)
    assert (h.status, h.problems) == ("warn", ["загрузок ещё не было"])


@pytest.mark.parametrize(
    ("silent_for", "status"),
    [(2 * D, "ok"), (5 * D, "warn"), (7 * D, "fail")],  # порог у ежедневного паблика — 3 дня
)
def test_silence(silent_for, status):
    h = evaluate(facts(last_item_at=NOW - H - silent_for), NOW)
    assert h.status == status
    if status != "ok":
        assert h.problems == [f"нет новых постов {silent_for.days} дн., обычно — не дольше 3 дн."]


def test_silence_is_measured_at_the_last_successful_load():
    # Компьютер был выключен 10 дней: и загрузок, и постов в базе нет столько же. Паблик не замолчал.
    h = evaluate(facts(last_success_at=NOW - 10 * D, last_item_at=NOW - 10 * D - 5 * H), NOW)
    assert h.status == "ok"


@pytest.mark.parametrize(
    ("platform", "attempt_ago", "status"),
    [
        ("vk", 12 * H, "warn"),  # три запуска по 3 часа пропущены, остальные источники грузились
        ("vk", 25 * H, "fail"),
        ("vk", 5 * H, "ok"),
        ("site", 30 * H, "ok"),  # у сайтов два запуска в сутки: 28 часов — ещё норма
        ("site", 40 * H, "warn"),
    ],
)
def test_ingest_lag(platform, attempt_ago, status):
    f = facts(platform, last_attempt_at=NOW - attempt_ago, reference_at=NOW - 2 * H)
    h = evaluate(f, NOW)
    assert h.status == status
    if status != "ok":
        lag = attempt_ago - 2 * H
        assert h.problems == [
            f"загрузка не запускалась {health.fmt_span(lag)}, хотя другие источники за это время грузились"
        ]


def test_ingest_lag_ignores_a_switched_off_computer():
    # Компьютер был выключен сутки: последняя попытка и последняя чужая загрузка — обе вчерашние.
    assert evaluate(facts(last_attempt_at=NOW - 30 * H, reference_at=NOW - 29 * H), NOW).status == "ok"


def test_posts_without_events():
    h = evaluate(facts(posts_30d=12, mentions_30d=0), NOW)
    assert (h.status, h.problems) == ("warn", ["постов за 30 дней: 12, событий в них: 0"])
    assert evaluate(facts(posts_30d=9, mentions_30d=0), NOW).status == "ok"  # мало постов — не повод


def test_site_is_judged_by_upcoming_shows_not_by_silence():
    quiet = facts("site", last_item_at=NOW - 40 * D)  # новых показов давно нет, но афиша на месяцы вперёд
    assert evaluate(quiet, NOW).status == "ok"
    empty = evaluate(facts("site", upcoming=0), NOW)
    assert (empty.status, empty.problems) == ("warn", ["в каталоге нет ни одного будущего показа"])


def test_fail_problems_come_first():
    h = evaluate(facts(failures_in_row=1, last_item_at=NOW - 8 * D, posts_30d=12, mentions_30d=0), NOW)
    assert h.status == "fail"
    assert h.problems[0].startswith("нет новых постов")
    assert len(h.problems) == 3


def test_checked_on_is_the_city_day():
    late_evening = datetime(2026, 11, 14, 22, 30, tzinfo=UTC)  # в Москве уже 15-е
    assert evaluate(facts(), late_evening).checked_on == date(2026, 11, 15)


def test_report_and_summary():
    results = [
        evaluate(facts(), NOW),
        evaluate(facts(failures_in_row=3, last_error="boom"), NOW),
    ]
    results[1].handle = "broken"
    text = health.report(results)
    assert text.splitlines()[0].startswith("fail  vk/broken")
    assert health.summary(results)["failed"] == ["vk/broken: загрузок с ошибкой подряд: 3. Последняя: boom"]
    results[1].facts.label = "Культ-клуб «Атмосфера»"
    assert health.report(results).splitlines()[0] == (
        "fail  vk/broken  Культ-клуб «Атмосфера» — загрузок с ошибкой подряд: 3. Последняя: boom"
    )
    assert health.summary(results)["failed"][0].startswith("vk/broken (Культ-клуб «Атмосфера»): ")


# --- На базе ---


def log_runs(conn, source_id, statuses, dag_id="ingest_vk"):
    """Запуски от старых к новым, раз в три часа до NOW."""
    with conn, conn.cursor() as cur:
        for i, status in enumerate(statuses):
            started = NOW - 3 * H * (len(statuses) - i)
            error = "HTTP 500" if status == "failed" else None
            cur.execute(
                """
                INSERT INTO meta.ingest_log (dag_id, run_id, source_id, status, started_at, finished_at, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (dag_id, f"run_{i}", source_id, status, started, started + H, error),
            )


def stored(conn):
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, status, failures_in_row, posts_30d, mentions_30d, upcoming
              FROM meta.source_health_latest ORDER BY handle
            """
        )
        return cur.fetchall()


def test_run_on_database(conn, lipetsk):
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.sources (city_id, platform, handle, note)
            SELECT city_id, 'site', 'teatr48.ru', 'Театр «На Соколе»' FROM meta.cities WHERE slug = 'lipetsk'
            RETURNING source_id
            """
        )
        site_id = cur.fetchone()[0]
        cur.execute("UPDATE meta.sources SET title = 'Тестовый паблик' WHERE source_id = %s", (lipetsk["source_id"],))

    posts = [make_post(i, days_ago=11 - i, text=f"Анонс {i}") for i in range(1, 11)]
    load(conn, lipetsk, posts)
    upcoming = (NOW.date() + timedelta(days=5)).isoformat()
    pipeline(conn, FakeLLM({"Анонс 10": [ev("Квиз", upcoming, "19:00")]}))
    # Паблик: успех, а потом три падения подряд. Запуск, который идёт прямо сейчас, не считается.
    log_runs(conn, lipetsk["source_id"], ["success", "success", "failed", "failed", "failed", "running"])
    log_runs(conn, site_id, ["success"], dag_id="ingest_sites")

    results = {h.handle: h for h in health.run(conn, now=NOW)}
    assert results["test_public"].status == "fail"
    assert results["test_public"].facts.last_item_at == datetime.fromtimestamp(posts[-1]["date"], tz=UTC)
    assert results["teatr48.ru"].problems == ["в каталоге нет ни одного будущего показа"]
    assert stored(conn) == [
        ("Театр «На Соколе»", "warn", 0, None, None, 0),
        ("Тестовый паблик", "fail", 3, 10, 1, 1),
    ]

    # Паблик починился: повторная проверка в тот же день переписывает строку дня.
    log_runs(conn, lipetsk["source_id"], ["success"], dag_id="ingest_vk_manual")
    health.run(conn, now=NOW + H)
    assert stored(conn)[1][:3] == ("Тестовый паблик", "ok", 0)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM meta.source_health")
        assert cur.fetchone()[0] == 2


def test_archived_posts_still_count_for_habits(conn, lipetsk):
    # Паблик пишет раз в неделю. Через 70 дней старые посты уже в архиве, но привычки видны по нему.
    posts = [make_post(i, days_ago=29 - 7 * (i - 1)) for i in range(1, 5)]  # 29, 22, 15, 8 дней назад
    load(conn, lipetsk, posts)
    later = NOW + timedelta(days=40)
    archive.run(conn, now=later + timedelta(days=30))
    with conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM archive.vk_posts")
        assert cur.fetchone()[0] == 3  # последний пост остаётся в raw
    (h,) = [r for r in health.run(conn, now=later) if r.handle == "test_public"]
    assert len(h.facts.item_times) == 4
    assert h.facts.last_item_at == datetime.fromtimestamp(posts[-1]["date"], tz=UTC)


def log_run(conn, source_id, run_id, started, *, minutes=5, status="success"):
    finished = started + timedelta(minutes=minutes) if status != "running" else None
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.ingest_log (dag_id, run_id, source_id, status, started_at, finished_at)
            VALUES ('ingest', %s, %s, %s, %s, %s)
            """,
            (run_id, source_id, status, started, finished),
        )


def lag_problems(conn, now):
    (h,) = [r for r in health.run(conn, now=now) if r.handle == "test_public"]
    return [p for p in h.problems if p.startswith("загрузка не запускалась")]


def test_stalled_dag_is_noticed_and_boot_is_not(conn, lipetsk):
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.sources (city_id, platform, handle)
            SELECT city_id, 'site', 'teatr48.ru' FROM meta.cities WHERE slug = 'lipetsk'
            RETURNING source_id
            """
        )
        site_id = cur.fetchone()[0]
    log_run(conn, lipetsk["source_id"], "vk_old", NOW - 20 * H)

    # Компьютер включили полчаса назад: сайт уже загрузился, паблик ещё нет. Это не отставание.
    log_run(conn, site_id, "site_boot", NOW - 30 * timedelta(minutes=1))
    assert lag_problems(conn, NOW) == []

    # А если сайт грузился и пять часов назад, Airflow работал, а паблик — нет.
    log_run(conn, site_id, "site_earlier", NOW - 5 * H)
    assert lag_problems(conn, NOW) == ["загрузка не запускалась 15 ч, хотя другие источники за это время грузились"]

    # Зависший запуск паблика — тоже попытка: пока он свежий, тревоги нет.
    log_run(conn, lipetsk["source_id"], "vk_stuck", NOW - 4 * H, status="running")
    assert lag_problems(conn, NOW) == []
    # Запуск так и висит, а сайты за это время снова грузились: зависание видно.
    log_run(conn, site_id, "site_later", NOW + 15 * H)
    assert lag_problems(conn, NOW + 20 * H) == [
        "загрузка не запускалась 19 ч, хотя другие источники за это время грузились"
    ]
