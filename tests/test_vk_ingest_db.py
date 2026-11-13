"""Загрузка в raw.vk_posts на настоящей базе: инкремент, идемпотентность, правки."""

import pytest

from conftest import NOW, FakeVK, make_post
from funfinder.vk.ingest import IngestSettings, SourceUnavailable, ingest_source, resolve_source

SETTINGS = IngestSettings(page_size=5, initial_lookback_days=30, recheck_days=3)


def run(vk, conn, source, run_id="run_1", now=NOW):
    return ingest_source(vk, conn, source, run_id=run_id, settings=SETTINGS, now=now)


def rows(conn):
    with conn, conn.cursor() as cur:
        cur.execute("SELECT post_id, text, changed_at IS NOT NULL, ingest_run_id FROM raw.vk_posts ORDER BY post_id")
        return cur.fetchall()


def test_first_load_and_idempotent_rerun(conn, lipetsk):
    vk = FakeVK([make_post(i, days_ago=40 - 3 * i) for i in range(1, 13)])

    first = run(vk, conn, lipetsk)
    assert first.inserted == first.fetched > 0
    assert first.updated == 0
    stored = rows(conn)
    assert len(stored) == first.inserted

    again = run(vk, conn, lipetsk, run_id="run_2")
    assert (again.inserted, again.updated) == (0, 0)
    assert rows(conn) == stored  # ничего не поменялось, даже ingest_run_id


def test_new_posts_and_edits_are_picked_up(conn, lipetsk):
    posts = [make_post(i, days_ago=10 - i) for i in range(1, 10)]
    vk = FakeVK(posts)
    run(vk, conn, lipetsk)

    # Организатор поправил свежий пост и выложил новый.
    posts[-1]["text"] = "Перенос на 15 ноября"
    posts.append(make_post(10, days_ago=0.1))
    vk = FakeVK(posts)

    result = run(vk, conn, lipetsk, run_id="run_2")
    assert (result.inserted, result.updated) == (1, 1)
    edited = {r[0]: r for r in rows(conn)}[9]
    assert edited[1] == "Перенос на 15 ноября"
    assert edited[2] is True  # changed_at заполнен
    assert edited[3] == "run_2"


def test_pinned_duplicate_does_not_break_upsert(conn, lipetsk):
    vk = FakeVK([make_post(1, days_ago=1, is_pinned=1), make_post(2, days_ago=0.5)])
    vk.wall.append(vk.wall[0])  # тот же пост ещё раз в ленте
    result = run(vk, conn, lipetsk)
    assert result.inserted == 2


def test_people_ids_not_stored(conn, lipetsk):
    vk = FakeVK([make_post(1, days_ago=1, from_id=555, signer_id=556)])
    run(vk, conn, lipetsk)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT payload FROM raw.vk_posts")
        payload = cur.fetchone()[0]
    assert "signer_id" not in payload and "from_id" not in payload


def test_resolve_source_saves_group_id(conn, lipetsk):
    source = {**lipetsk, "external_id": None}
    vk = FakeVK([], group={"id": 4242, "name": "Афиша", "is_closed": 0})
    resolved = resolve_source(vk, conn, source)
    assert resolved["external_id"] == 4242
    with conn, conn.cursor() as cur:
        cur.execute("SELECT external_id, title FROM meta.sources WHERE source_id = %s", (lipetsk["source_id"],))
        assert cur.fetchone() == (4242, "Афиша")
    # Второй раз в API не ходим.
    vk.calls.clear()
    resolve_source(vk, conn, resolved)
    assert vk.calls == []


@pytest.mark.parametrize("group", [{"id": 1, "is_closed": 1}, {"id": 1, "deactivated": "banned"}])
def test_resolve_source_rejects_unavailable(conn, lipetsk, group):
    with pytest.raises(SourceUnavailable):
        resolve_source(FakeVK([], group=group), conn, {**lipetsk, "external_id": None})
