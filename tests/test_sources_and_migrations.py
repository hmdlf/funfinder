from pathlib import Path

import pytest

from funfinder import ingest_log
from funfinder.db.migrate import MigrationError, migrate
from funfinder.sources import SourcesConfigError, load_all, load_city_config, sync_city

REPO_SOURCES = Path(__file__).resolve().parents[1] / "sources"


def write(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


CITY = """
city: {slug: lipetsk, name: Липецк, timezone: Europe/Moscow}
vk:
  - handle: https://vk.com/First_Public
    note: Первый
  - handle: second
    settings: {initial_lookback_days: 60}
"""


def test_repo_sources_are_valid():
    assert {c.slug for c in load_all(REPO_SOURCES)} >= {"lipetsk"}


def test_load_city_config_normalizes(tmp_path):
    cfg = load_city_config(write(tmp_path, "lipetsk.yaml", CITY))
    assert [s.handle for s in cfg.vk] == ["first_public", "second"]
    assert cfg.vk[1].settings == {"initial_lookback_days": 60}


@pytest.mark.parametrize(
    "body",
    [
        "city: {slug: Lipetsk, name: L, timezone: Europe/Moscow}",
        "city: {slug: lipetsk, name: L, timezone: Mars/Base}",
        "city: {slug: lipetsk, name: L, timezone: Europe/Moscow}\nvk: [{handle: a}, {handle: A}]",
        "city: {slug: lipetsk, name: L, timezone: Europe/Moscow}\nvk: [{handle: a, settings: {foo: 1}}]",
        "city: [unclosed",
    ],
)
def test_bad_configs_rejected(tmp_path, body):
    with pytest.raises(SourcesConfigError):
        load_city_config(write(tmp_path, "x.yaml", body))


def test_sync_is_idempotent_and_deactivates_removed(conn, tmp_path):
    cfg = load_city_config(write(tmp_path, "lipetsk.yaml", CITY))
    assert sync_city(conn, cfg) == {
        "city": "lipetsk",
        "vk_sources": 2,
        "tg_sources": 0,
        "site_sources": 0,
        "deactivated": 0,
        "venues": 0,
    }
    assert sync_city(conn, cfg)["deactivated"] == 0

    shorter = load_city_config(write(tmp_path, "lipetsk.yaml", CITY.split("  - handle: second")[0]))
    assert sync_city(conn, shorter)["deactivated"] == 1
    with conn, conn.cursor() as cur:
        cur.execute("SELECT handle, is_active FROM meta.sources ORDER BY handle")
        assert cur.fetchall() == [("first_public", True), ("second", False)]


def test_migrations_are_repeatable(conn):
    assert migrate(conn) == []


def test_changed_migration_is_detected(conn, tmp_path):
    write(tmp_path, "900_tmp.sql", "CREATE TABLE meta.tmp_check (x int);")
    try:
        assert migrate(conn, tmp_path) == ["900_tmp"]
        write(tmp_path, "900_tmp.sql", "CREATE TABLE meta.tmp_check (x bigint);")
        with pytest.raises(MigrationError):
            migrate(conn, tmp_path)
    finally:
        with conn, conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS meta.tmp_check")
            cur.execute("DELETE FROM meta.schema_migrations WHERE version = '900_tmp'")


def test_ingest_log_totals(conn, lipetsk):
    run = {"dag_id": "ingest_vk", "run_id": "r1", "source_id": lipetsk["source_id"]}
    ingest_log.log_start(conn, attempt=1, **run)
    ingest_log.log_finish(conn, status="failed", error="timeout", **run)
    ingest_log.log_start(conn, attempt=2, **run)  # ретрай перезаписывает строку
    ingest_log.log_finish(conn, status="success", stats={"pages": 1, "fetched": 3, "inserted": 2, "updated": 1}, **run)
    assert ingest_log.run_totals(conn, dag_id="ingest_vk", run_id="r1") == {
        "sources_ok": 1,
        "sources_failed": 0,
        "inserted": 2,
        "updated": 1,
    }
