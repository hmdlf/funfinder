"""Telegram на настоящем Postgres: загрузка в raw.tg_posts и извлечение событий из постов каналов."""

from datetime import UTC, datetime, timedelta

import pytest
from tg_pages import FakeChannel, Post, missing_channel_page

from funfinder.events import extract
from funfinder.events.llm import LLMResult
from funfinder.tg.ingest import TGSettings, ingest_channel
from funfinder.tg.page import ChannelUnavailable

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
FAST = TGSettings(page_delay_s=0)


def at(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


@pytest.fixture
def sinitsa(conn, lipetsk):
    """Telegram-канал в том же городе, что и VK-источник из фикстуры lipetsk."""
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.sources (city_id, platform, handle)
            SELECT city_id, 'tg', 'sinitsabar' FROM meta.cities WHERE slug = 'lipetsk'
            RETURNING source_id
            """
        )
        source_id = cur.fetchone()[0]
    return {"source_id": source_id, "handle": "sinitsabar", "settings": {}}


def ingest(conn, channel, source, now=NOW):
    return ingest_channel(channel, conn, source, run_id="test", settings=FAST, now=now, sleep=lambda _: None)


def rows(conn):
    with conn, conn.cursor() as cur:
        cur.execute("SELECT post_id, text, reply_text, media_count FROM raw.tg_posts ORDER BY post_id")
        return cur.fetchall()


def test_first_load_rerun_and_edits(conn, sinitsa):
    channel = FakeChannel(
        "sinitsabar",
        [Post(100 + i, at(40 - i), f"Пост {i}") for i in range(40)],  # по посту в день, 40 дней
    )
    first = ingest(conn, channel, sinitsa)
    assert (first.inserted, first.updated) == (30, 0)  # только последние 30 дней
    assert ingest(conn, channel, sinitsa).as_dict() == {"pages": 1, "fetched": 7, "inserted": 0, "updated": 0}

    # Новый пост и правка вчерашнего: правка ловится перепроверкой последней недели.
    channel.posts[-1].text_html = "Пост 39, исправлено: начало в 20:00"
    channel.posts.append(Post(140, at(0), "Пост 40"))
    again = ingest(conn, channel, sinitsa)
    assert (again.inserted, again.updated) == (1, 1)
    assert rows(conn)[-2][1] == "Пост 39, исправлено: начало в 20:00"


def test_reply_and_media_are_stored(conn, sinitsa):
    channel = FakeChannel(
        "sinitsabar",
        [
            Post(10, at(2), "Киночетверг: 12.11 – «Престиж»", photos=1, videos=4),
            Post(11, at(1), "Сегодня в 20:00", reply_html="Киночетверг: 12.11 – «Престиж»"),
        ],
    )
    ingest(conn, channel, sinitsa)
    assert rows(conn) == [
        (10, "Киночетверг: 12.11 – «Престиж»", "", 5),
        (11, "Сегодня в 20:00", "Киночетверг: 12.11 – «Престиж»", 0),
    ]


def test_closed_channel(conn, sinitsa):
    class Closed:
        def get(self, url):
            return missing_channel_page()

    with pytest.raises(ChannelUnavailable):
        ingest(conn, Closed(), sinitsa)


class RecordingLLM:
    """Поддельная модель: запоминает сообщения, событие придумывает по тексту."""

    def __init__(self):
        self.messages = []

    def extract(self, system_prompt, message):
        self.messages.append(message)
        return LLMResult(
            status="ok",
            model="fake",
            events=[{"title": "Престиж", "date": "2026-11-12", "time": "20:00", "venue": None, "category": "cinema"}],
        )


def test_extract_reads_telegram_posts(conn, sinitsa):
    ingest(
        conn, FakeChannel("sinitsabar", [Post(11, at(1), "Сегодня в 20:00", reply_html="12.11 – «Престиж»")]), sinitsa
    )
    llm = RecordingLLM()
    stats = extract.run(conn, llm, now=NOW)
    assert stats["posts"] == 1 and stats["events"] == 1
    # Цитата поста, на который ответили, идёт в модель вместе с текстом.
    assert llm.messages[0].endswith("Сегодня в 20:00\n\n[Ответ на пост]\n12.11 – «Престиж»")

    with conn, conn.cursor() as cur:
        cur.execute("SELECT source_id, post_id, source_url, title FROM staging.events")
        assert cur.fetchall() == [(sinitsa["source_id"], 11, "https://t.me/sinitsabar/11", "Престиж")]
        cur.execute("SELECT source_id, post_id, status FROM staging.post_extractions")
        assert cur.fetchall() == [(sinitsa["source_id"], 11, "ok")]

    assert extract.run(conn, llm, now=NOW)["posts"] == 0  # второй раз пост в модель не идёт


def test_sync_keeps_platforms_apart(conn, tmp_path):
    from funfinder.sources import load_city_config, sync_city

    path = tmp_path / "lipetsk.yaml"
    city = "city: {slug: lipetsk, name: Липецк, timezone: Europe/Moscow}\nvk:\n  - handle: club1\n"
    path.write_text(city + "tg:\n  - handle: sinitsabar\n", encoding="utf-8")
    assert sync_city(conn, load_city_config(path))["tg_sources"] == 1

    # Канал убрали из yaml: неактивным становится только он, VK-источник не трогаем.
    path.write_text(city, encoding="utf-8")
    assert sync_city(conn, load_city_config(path))["deactivated"] == 1
    with conn, conn.cursor() as cur:
        cur.execute("SELECT platform, handle, is_active FROM meta.sources ORDER BY platform")
        assert cur.fetchall() == [("tg", "sinitsabar", False), ("vk", "club1", True)]
