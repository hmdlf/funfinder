"""Общие фикстуры.

Тесты с базой используют отдельную базу funfinder_test (её создаёт
docker/postgres-init). Строка подключения — FUNFINDER_TEST_DSN; в
контейнере airflow-cli она уже задана. Без неё тесты с базой пропускаются.
"""

import json
import os
from datetime import UTC, date, datetime, timedelta

import psycopg2
import pytest

from funfinder.db.migrate import migrate
from funfinder.events import dedupe, extract, validate, venues
from funfinder.events.dedupe import norm_title
from funfinder.events.llm import LLMResult
from funfinder.publish import run
from funfinder.vk.ingest import IngestSettings, ingest_source

SCHEMAS = ("meta", "raw", "staging", "catalog", "quarantine", "archive")


@pytest.fixture(scope="session")
def db_dsn():
    dsn = os.environ.get("FUNFINDER_TEST_DSN")
    if not dsn:
        pytest.skip("FUNFINDER_TEST_DSN не задана — тесты с базой пропущены")
    if "test" not in dsn.rsplit("/", 1)[-1]:
        # Тесты сносят схемы целиком. Защита от запуска на рабочей базе.
        pytest.exit(f"FUNFINDER_TEST_DSN должна указывать на тестовую базу, а не на {dsn.rsplit('/', 1)[-1]}")
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        for schema in SCHEMAS:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    migrate(conn)
    conn.close()
    return dsn


@pytest.fixture
def conn(db_dsn):
    conn = psycopg2.connect(db_dsn)
    with conn, conn.cursor() as cur:
        cur.execute("TRUNCATE raw.vk_posts, meta.ingest_log, meta.sources, meta.cities RESTART IDENTITY CASCADE")
        cur.execute("TRUNCATE meta.oauth_tokens")
        # У архива нет внешних ключей, CASCADE до него не доходит.
        cur.execute(
            "TRUNCATE archive.events, archive.mentions, archive.vk_posts, archive.tg_posts,"
            " archive.post_extractions, archive.site_events, archive.event_pages"
        )
    yield conn
    conn.close()


@pytest.fixture
def lipetsk(conn):
    """Город и один VK-источник с уже известным id сообщества."""
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.cities (slug, name, timezone)
            VALUES ('lipetsk', 'Липецк', 'Europe/Moscow')
            RETURNING city_id
            """
        )
        city_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO meta.sources (city_id, platform, handle, external_id)
            VALUES (%s, 'vk', 'test_public', 1001) RETURNING source_id
            """,
            (city_id,),
        )
        source_id = cur.fetchone()[0]
    return {"source_id": source_id, "handle": "test_public", "external_id": 1001, "settings": {}, "city": "lipetsk"}


NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def ts(days_ago: float) -> int:
    return int(NOW.timestamp() - days_ago * 86400)


def make_post(post_id: int, days_ago: float, text: str = "", **extra) -> dict:
    return {
        "id": post_id,
        "owner_id": -1001,
        "from_id": -1001,
        "date": ts(days_ago),
        "text": text or f"пост {post_id}",
        **extra,
    }


class FakeVK:
    """Стена в памяти: отдаёт посты страницами, как wall.get."""

    def __init__(self, posts: list[dict], group: dict | None = None):
        pinned = [p for p in posts if p.get("is_pinned")]
        rest = sorted((p for p in posts if not p.get("is_pinned")), key=lambda p: p["date"], reverse=True)
        self.wall = pinned + rest
        self.group = group or {"id": 1001, "name": "Тестовый паблик", "screen_name": "test_public", "is_closed": 0}
        self.calls: list[tuple] = []

    def wall_get(self, owner_id, *, offset, count, wall_filter="owner"):
        self.calls.append(("wall.get", owner_id, offset, count))
        return self.wall[offset : offset + count]

    def get_group(self, handle):
        self.calls.append(("groups.getById", handle))
        return self.group


# --- process_events без LLM: общие помощники тестов с базой ---

SETTINGS = IngestSettings(page_size=50, initial_lookback_days=30, recheck_days=3)


class FakeLLM:
    """Вместо LLM: отвечает заранее заданными событиями по ключевой фразе в посте."""

    def __init__(self, answers: dict):
        self.answers = answers  # фраза в посте → события, "REFUSED", "INVALID" или "TIMEOUT"
        self.messages: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.messages)

    def extract(self, system_prompt: str, user_message: str) -> LLMResult:
        self.messages.append(user_message)
        for key, answer in self.answers.items():
            if key in user_message:
                if answer == "REFUSED":
                    return LLMResult(status="refused")
                if answer == "TIMEOUT":
                    return LLMResult(status="timeout")
                if answer == "INVALID":
                    return LLMResult(status="invalid_json", model="fake", raw_answer='{"events": [}')
                raw = json.dumps({"events": answer}, ensure_ascii=False)
                return LLMResult(status="ok", model="fake", events=answer, raw_answer=raw, tokens_in=100)
        return LLMResult(status="ok", model="fake", raw_answer='{"events": []}')


def ev(title, day, at=None, venue=None, category="quiz", status="scheduled"):
    """Событие так, как его вернула бы модель."""
    return {
        "title": title,
        "date": day,
        "time": at,
        "venue": venue,
        "price": None,
        "category": category,
        "status": status,
    }


def load(conn, source, posts, run_id="run_1"):
    ingest_source(FakeVK(posts), conn, source, run_id=run_id, settings=SETTINGS, now=NOW)


def pipeline(conn, llm):
    """Как process_events: extract → validate → resolve_venues → dedupe."""
    extracted, validated = extract.run(conn, llm, now=NOW), validate.run(conn)
    venues.run(conn)
    return extracted, validated, dedupe.run(conn)


# --- Публикация подборки ---

# Витрина показывает только будущее, поэтому в тестах выпуска даты — от настоящего «сегодня».
REAL_TODAY = date.today()
REAL_TOMORROW = REAL_TODAY + timedelta(days=1)
ISSUE = run.Plan(REAL_TOMORROW, REAL_TODAY, late=False)


class FakeSender:
    """Отправитель для run.publish без сети: запоминает, что и куда ушло."""

    def __init__(self, fail: Exception | None = None):
        self.fail, self.sent, self.keys = fail, [], []

    def send(self, handle, text, *, key=None, link=None):
        if self.fail:
            raise self.fail
        self.sent.append((handle, text))
        self.keys.append((key, link))
        return f"m{len(self.sent)}"


def add_events(conn, source, rows):
    """События каталога с упоминанием в посте VK: (название, день, время, категория, статус)."""
    with conn, conn.cursor() as cur:
        cur.execute("SELECT city_id FROM meta.cities WHERE slug = 'lipetsk'")
        city_id = cur.fetchone()[0]
        for title, day, at, category, status in rows:
            cur.execute(
                """
                INSERT INTO catalog.events (city_id, title, title_norm, event_date, event_time, venue, category, status)
                VALUES (%s, %s, %s, %s, %s, 'Клуб «Атмосфера»', %s, %s) RETURNING event_id
                """,
                (city_id, title, norm_title(title), day, at, category, status),
            )
            event_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO staging.events (source_id, city_id, post_id, source_url, published_at,
                                            title, event_date, validated, event_id)
                VALUES (%s, %s, %s, %s, now(), %s, %s, true, %s)
                """,
                (source["source_id"], city_id, event_id, f"https://vk.com/wall-1_{event_id}", title, day, event_id),
            )
