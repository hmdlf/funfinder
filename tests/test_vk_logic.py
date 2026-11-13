"""Логика загрузки без базы и сети."""

from datetime import timedelta

import pytest

from conftest import NOW, FakeVK, make_post
from funfinder.vk.ingest import (
    IngestSettings,
    collect_posts,
    content_hash,
    normalize_handle,
    post_texts,
    sanitize_payload,
)

SETTINGS = IngestSettings(page_size=3, max_pages=10)


def collect(vk, watermark, days):
    return collect_posts(
        lambda offset, count: vk.wall_get(-1001, offset=offset, count=count),
        watermark=watermark,
        cutoff=NOW - timedelta(days=days),
        settings=SETTINGS,
    )


def ids(posts):
    return sorted(p["id"] for p in posts)


def test_first_load_takes_lookback_window_and_stops():
    vk = FakeVK([make_post(i, days_ago=10 - i) for i in range(1, 11)])  # id 10 — самый свежий
    posts, pages = collect(vk, watermark=None, days=4.5)
    assert ids(posts) == [6, 7, 8, 9, 10]
    assert pages == 2  # на второй странице встретили пост старше окна и остановились


def test_incremental_takes_only_new_ids_outside_window():
    vk = FakeVK([make_post(i, days_ago=20 - 2 * i) for i in range(1, 11)])  # посту 8 — 4 дня
    # В базе уже есть всё до id 8. Окно перепроверки — 3 дня.
    posts, _ = collect(vk, watermark=8, days=3)
    assert ids(posts) == [9, 10]  # 8 уже есть и вне окна — на нём остановились


def test_incremental_rechecks_known_posts_inside_window():
    vk = FakeVK([make_post(i, days_ago=20 - 2 * i) for i in range(1, 11)])
    # Всё уже загружено, но посты моложе 3 дней перечитываем: вдруг их поправили.
    posts, _ = collect(vk, watermark=10, days=3)
    assert ids(posts) == [9, 10]


def test_old_new_post_beyond_window_is_still_taken():
    # Планировщик стоял неделю: новые посты старше окна перепроверки, но их id больше водяного знака.
    vk = FakeVK([make_post(i, days_ago=30 - i) for i in range(1, 21)])
    posts, _ = collect(vk, watermark=15, days=3)
    assert ids(posts) == [16, 17, 18, 19, 20]


def test_pinned_old_post_does_not_stop_scan():
    posts_on_wall = [make_post(1, days_ago=100, is_pinned=1)] + [make_post(i, days_ago=10 - i) for i in range(2, 9)]
    vk = FakeVK(posts_on_wall)
    posts, _ = collect(vk, watermark=None, days=5)
    assert 1 not in ids(posts)
    assert ids(posts) == [5, 6, 7, 8]


def test_duplicate_post_in_feed_is_collected_once():
    vk = FakeVK([make_post(5, days_ago=1)])
    vk.wall = [vk.wall[0], vk.wall[0]]  # закреплённый может прийти и вторым экземпляром
    posts, _ = collect(vk, watermark=None, days=5)
    assert ids(posts) == [5]


def test_max_pages_limit():
    vk = FakeVK([make_post(i, days_ago=0.1) for i in range(1, 31)])
    posts, pages = collect_posts(
        lambda o, c: vk.wall_get(-1001, offset=o, count=c),
        watermark=None,
        cutoff=NOW - timedelta(days=5),
        settings=IngestSettings(page_size=3, max_pages=2),
    )
    assert pages == 2 and len(posts) == 6


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("lipetsk_afisha", "lipetsk_afisha"),
        ("https://vk.com/Lipetsk_Afisha/", "lipetsk_afisha"),
        ("https://m.vk.com/club123?w=wall-123_1", "club123"),
        ("vk.ru/public456", "public456"),
        ("@some.page", "some.page"),
    ],
)
def test_normalize_handle(raw, expected):
    assert normalize_handle(raw) == expected


@pytest.mark.parametrize("raw", ["", "https://t.me/channel", "vk.com/группа"])
def test_normalize_handle_rejects_garbage(raw):
    with pytest.raises(ValueError):
        normalize_handle(raw)


def test_sanitize_removes_people_but_keeps_communities():
    post = {
        "id": 1,
        "from_id": 777,
        "signer_id": 888,
        "owner_id": -1001,
        "copy_history": [{"id": 5, "from_id": 999, "owner_id": -2002}, {"id": 6, "from_id": -3003}],
    }
    clean = sanitize_payload(post)
    assert "from_id" not in clean and "signer_id" not in clean
    assert clean["owner_id"] == -1001
    assert "from_id" not in clean["copy_history"][0]
    assert clean["copy_history"][1]["from_id"] == -3003
    assert post["from_id"] == 777  # исходный словарь не тронут


def test_texts_and_hash():
    post = {"text": "Анонс", "copy_history": [{"text": "Концерт 12 ноября"}]}
    text, repost = post_texts(post)
    assert (text, repost) == ("Анонс", "Концерт 12 ноября")
    assert content_hash(text, repost) != content_hash(text + repost, "")


def test_settings_validation():
    assert IngestSettings.from_overrides({"recheck_days": 5}).recheck_days == 5
    with pytest.raises(ValueError, match="неизвестные"):
        IngestSettings.from_overrides({"recheck_dayz": 5})
    with pytest.raises(ValueError):
        IngestSettings.from_overrides({"wall_filter": "others"})
    with pytest.raises(ValueError):
        IngestSettings.from_overrides({"page_size": 500})
