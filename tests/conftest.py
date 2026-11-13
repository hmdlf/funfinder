"""Общие фикстуры.

Тесты с базой используют отдельную базу funfinder_test (её создаёт
docker/postgres-init). Строка подключения — FUNFINDER_TEST_DSN; в
контейнере airflow-cli она уже задана. Без неё тесты с базой пропускаются.
"""

import os
from datetime import UTC, datetime

import psycopg2
import pytest

from funfinder.db.migrate import migrate

SCHEMAS = ("meta", "raw", "staging", "catalog", "quarantine")


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
