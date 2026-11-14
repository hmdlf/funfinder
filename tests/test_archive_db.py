"""archive на настоящем Postgres: что уходит, что остаётся, повторы, пересборка, водяной знак."""

from datetime import timedelta

import psycopg2
import pytest

from conftest import NOW, SETTINGS, FakeLLM, FakeVK, ev, load, make_post, pipeline
from funfinder.db import lock_catalog
from funfinder.events import dedupe, extract
from funfinder.ops import archive
from funfinder.site import pages
from funfinder.vk.ingest import ingest_source

LATER = NOW + timedelta(days=75)  # архив смотрит с этого дня: граница — NOW + 15 дней


def day(offset: int) -> str:
    return (NOW.date() + timedelta(days=offset)).isoformat()


POSTS = [
    make_post(1, days_ago=20, text="Анонс квиза"),
    make_post(2, days_ago=19, text="Напоминание: квиз"),
    make_post(3, days_ago=18, text="Большой концерт весной"),
    make_post(4, days_ago=17, text="Фотоотчёт"),
    make_post(5, days_ago=16, text="Просто мем"),
    make_post(6, days_ago=1, text="Свежий анонс"),
]

LLM = {
    "квиз": [ev("Квиз «Кино»", day(-15), "19:00")],
    "Большой концерт": [ev("Весенний концерт", day(100), "18:00", category="concert")],
    "Фотоотчёт": [ev("Фестиваль", day(-30), category="festival")],  # дата раньше поста → карантин
    "Свежий анонс": [ev("Лекция", day(3), "17:00", category="lecture")],
}


def counts(conn) -> dict:
    tables = [
        "catalog.events", "staging.events", "raw.vk_posts", "staging.post_extractions",
        "archive.events", "archive.mentions", "archive.vk_posts", "archive.post_extractions",
    ]  # fmt: skip
    with conn, conn.cursor() as cur:
        result = {}
        for table in tables:
            cur.execute(f"SELECT count(*) FROM {table}")
            result[table] = cur.fetchone()[0]
    return result


@pytest.fixture
def filled(conn, lipetsk):
    load(conn, lipetsk, POSTS)
    pipeline(conn, FakeLLM(LLM))
    return lipetsk


def test_past_goes_to_archive_and_live_stays(conn, filled):
    stats = archive.run(conn, now=LATER)
    assert stats == {
        "mentions": 3,  # квиз (анонс и напоминание) и лекция
        "events": 2,
        "quarantined": 1,  # фотоотчёт
        "vk_posts": 4,  # 1, 2, 4, 5: упоминаний не осталось
        "tg_posts": 0,
        "post_extractions": 4,
        "site_events": 0,
        "event_pages": 0,
    }
    with conn, conn.cursor() as cur:
        cur.execute("SELECT title FROM catalog.events")
        assert cur.fetchall() == [("Весенний концерт",)]  # анонс за 3 месяца — ещё впереди
        cur.execute("SELECT post_id FROM raw.vk_posts ORDER BY post_id")
        # 3 — его событие впереди; 6 — последний пост паблика, по нему считается водяной знак.
        assert cur.fetchall() == [(3,), (6,)]
        cur.execute("SELECT post_id FROM staging.post_extractions ORDER BY post_id")
        assert cur.fetchall() == [(3,), (6,)]
        cur.execute(
            """
            SELECT e.title, count(m.id)
              FROM archive.events e LEFT JOIN archive.mentions m USING (event_id)
             GROUP BY e.title ORDER BY e.title
            """
        )
        assert cur.fetchall() == [("Квиз «Кино»", 2), ("Лекция", 1)]  # lineage сохранился
        cur.execute("SELECT title, quarantine_reason FROM archive.mentions WHERE event_id IS NULL")
        assert cur.fetchall() == [("Фестиваль", "past_event")]
        cur.execute("SELECT count(*) FROM archive.events WHERE archived_at IS NULL")
        assert cur.fetchone()[0] == 0


def test_nothing_moves_before_the_cutoff(conn, filled):
    before = counts(conn)
    assert sum(archive.run(conn, now=NOW).values()) == 0  # всё моложе 60 дней
    assert counts(conn) == before


def test_second_run_moves_nothing(conn, filled):
    archive.run(conn, now=LATER)
    after = counts(conn)
    assert sum(archive.run(conn, now=LATER).values()) == 0
    assert counts(conn) == after


def test_rebuild_catalog_keeps_archive(conn, filled):
    archive.run(conn, now=LATER)
    archived = counts(conn)
    stats = dedupe.run(conn, rebuild=True)
    assert stats["created"] == 1  # пересобран только живой концерт
    assert counts(conn) == archived


def test_archived_posts_are_not_downloaded_again(conn, filled):
    archive.run(conn, now=LATER)
    # Та же стена через 75 дней: водяной знак (пост 6) на месте, старые посты не новые.
    result = ingest_source(FakeVK(POSTS), conn, filled, run_id="later", settings=SETTINGS, now=LATER)
    assert (result.inserted, result.updated) == (0, 0)
    llm = FakeLLM(LLM)
    extract.run(conn, llm, now=LATER)
    assert llm.calls == 0


def test_sites_pages_and_log_by_date(conn, lipetsk):
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.sources (city_id, platform, handle)
            SELECT city_id, 'site', 'teatr48.ru' FROM meta.cities WHERE slug = 'lipetsk'
            RETURNING source_id, city_id
            """
        )
        site_id, city_id = cur.fetchone()
        for key, offset in (("old", -61), ("edge", -60), ("next", 5)):
            cur.execute(
                """
                INSERT INTO raw.site_events (source_id, event_key, title, event_date, url, content_hash)
                VALUES (%s, %s, 'Спектакль', %s, 'https://teatr48.ru/', 'h')
                """,
                (site_id, key, day(offset)),
            )
            cur.execute(
                """
                INSERT INTO catalog.event_pages (city_id, slug, event_date, title, title_norm)
                VALUES (%s, %s, %s, 'Спектакль', 'спект')
                """,
                (city_id, f"{day(offset)}-spektakl", day(offset)),
            )
        for days_ago in (91, 89):
            cur.execute(
                """
                INSERT INTO meta.ingest_log (dag_id, run_id, source_id, status, started_at)
                VALUES ('ingest_sites', %s, %s, 'success', %s)
                """,
                (f"run_{days_ago}", site_id, NOW - timedelta(days=days_ago)),
            )

    stats = archive.run(conn, now=NOW)
    assert (stats["site_events"], stats["event_pages"]) == (1, 1)  # граница: ровно 60 дней ещё остаётся
    with conn, conn.cursor() as cur:
        cur.execute("SELECT event_key FROM archive.site_events")
        assert cur.fetchall() == [("old",)]
        cur.execute("SELECT slug FROM archive.event_pages")
        assert cur.fetchall() == [(f"{day(-61)}-spektakl",)]

    assert archive.trim_ingest_log(conn, now=NOW) == {"deleted": 1}
    with conn, conn.cursor() as cur:
        cur.execute("SELECT run_id FROM meta.ingest_log")
        assert cur.fetchall() == [("run_89",)]


def test_archive_tables_mirror_live_ones(conn):
    """Столбец, добавленный в рабочую таблицу, должен появиться и в архивной."""
    with conn, conn.cursor() as cur:
        for live, (target, _key) in archive.TABLES.items():
            live_cols = archive.columns(cur, live)
            archive_cols = archive.columns(cur, target)
            assert live_cols, live
            assert set(archive_cols) - set(live_cols) == {"archived_at"}, target
            assert set(live_cols) <= set(archive_cols), f"{target}: нет {set(live_cols) - set(archive_cols)}"


def test_cutoff_is_beyond_what_the_pipeline_still_touches():
    assert archive.ARCHIVE_AFTER_DAYS > extract.MAX_POST_AGE_DAYS  # окно извлечения и reextract
    assert archive.ARCHIVE_AFTER_DAYS > pages.GONE_DAYS  # страница «событие прошло»


def test_dedupe_waits_while_catalog_is_locked(conn, db_dsn, filled):
    other = psycopg2.connect(db_dsn)
    try:
        with other.cursor() as cur:  # транзакция открыта — блокировка держится, как во время архива
            lock_catalog(cur)
        with conn, conn.cursor() as cur:
            cur.execute("SET lock_timeout = '200ms'")
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            dedupe.run(conn)
    finally:
        other.rollback()
        other.close()
