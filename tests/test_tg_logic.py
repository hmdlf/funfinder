"""Telegram без базы и сети: разбор страницы, листание, имена каналов, команда для сервера."""

from datetime import UTC, datetime, timedelta

import pytest
from tg_pages import FakeChannel, Post, channel_page, missing_channel_page

from funfinder.sources import SourcesConfigError, load_city_config
from funfinder.tg import fetch
from funfinder.tg.ingest import TGSettings, collect_posts, normalize_channel
from funfinder.tg.page import ChannelUnavailable, PageFormatError, parse_page

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def at(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


# --- Разбор страницы ---


def test_parse_page_texts_media_and_reply():
    page = parse_page(
        channel_page(
            "sinitsabar",
            [
                Post(
                    2674,
                    at(4),
                    "<b>Киночетверг в Синице&#33;</b><br/><br/>12.11 – «Престиж» "
                    '<i class="emoji" style="background-image:url(x)"><b>🎬</b></i>',
                    photos=1,
                    videos=4,
                ),
                Post(
                    2685,
                    at(1),
                    'Сегодня смотрим любимый триллер в 20:00, бронь у <a href="https://t.me/x">Мариночки</a>',
                    reply_html="Киночетверг в Синице&#33;  12.11 – «Престиж»…",
                ),
                Post(2686, at(0.5), photos=3),  # альбом без подписи
                Post(2687, at(0.4), "Канал создан", service=True),
            ],
            before=2674,
        ),
        "sinitsabar",
    )
    assert page.before == 2674
    assert [p.post_id for p in page.posts] == [2674, 2685, 2686]  # служебное сообщение пропущено

    film, reply, album = page.posts
    assert film.text == "Киночетверг в Синице!\n\n12.11 – «Престиж» 🎬"  # <br> → перенос, эмодзи на месте
    assert film.media_count == 5
    assert film.reply_text == ""
    assert film.published_at == at(4)
    assert film.url == "https://t.me/sinitsabar/2674"

    assert reply.text == "Сегодня смотрим любимый триллер в 20:00, бронь у Мариночки"  # текст цитаты сюда не попал
    assert reply.reply_text.startswith("Киночетверг в Синице!")
    assert album.text == "" and album.media_count == 3


def test_last_page_has_no_before():
    assert parse_page(channel_page("sinitsabar", [Post(1, at(1), "Первый пост")]), "sinitsabar").before is None


def test_missing_channel():
    with pytest.raises(ChannelUnavailable):
        parse_page(missing_channel_page(), "no_such_channel")


def test_post_without_date_is_a_format_error():
    html = channel_page("sinitsabar", [Post(1, at(1), "текст")]).replace("<time", "<span").replace("</time>", "</span>")
    with pytest.raises(PageFormatError):
        parse_page(html, "sinitsabar")


# --- Листание ---


def channel_with(n: int, every_hours: float = 12) -> FakeChannel:
    """n постов, от новых к старым с шагом every_hours; id 1000 + i."""
    return FakeChannel("sinitsabar", [Post(1000 + i, at((n - i) * every_hours / 24), f"Пост {i}") for i in range(n)])


def collect(channel: FakeChannel, *, watermark=None, days=30, **settings):
    return collect_posts(
        lambda before: parse_page(channel.get(fetch.page_url(channel.name, before)), channel.name),
        watermark=watermark,
        cutoff=NOW - timedelta(days=days),
        settings=TGSettings(page_delay_s=0, **settings),
        sleep=lambda _: None,
    )


def test_first_load_stops_at_lookback():
    channel = channel_with(100)  # 50 дней, по два поста в день
    posts, pages = collect(channel)
    assert len(posts) == 60  # последние 30 дней по два поста, граница включительно
    assert all(p.published_at >= NOW - timedelta(days=30) for p in posts)
    assert pages == 4  # по 20 постов; самый старый на 3-й странице ровно на границе — нужна ещё одна
    assert channel.requests[1] == "https://t.me/s/sinitsabar?before=1080"


def test_incremental_load_rechecks_last_week():
    channel = channel_with(100)
    posts, pages = collect(channel, watermark=1099, days=7)
    assert pages == 1  # неделя — это 14 постов, одной страницы хватает
    assert [p.post_id for p in posts] == list(range(1086, 1100))


def test_max_pages_limits_a_long_gap():
    posts, pages = collect(channel_with(200, every_hours=1), days=30, max_pages=2)
    assert pages == 2 and len(posts) == 40


def test_same_page_twice_does_not_loop():
    channel = channel_with(30)
    stuck = FakeChannel("sinitsabar", channel.posts)
    stuck.get = lambda url: channel.get(fetch.page_url("sinitsabar"))  # на любой ?before= — первая страница
    posts, pages = collect(stuck)
    assert pages == 2 and len(posts) == 20


def test_short_channel():
    posts, pages = collect(channel_with(5))
    assert pages == 1 and len(posts) == 5


# --- Имена каналов и настройки ---


@pytest.mark.parametrize(
    "raw",
    ["sinitsabar", "@sinitsabar", "https://t.me/sinitsabar", "t.me/s/sinitsabar", "https://t.me/sinitsabar/2671/"],
)
def test_normalize_channel(raw):
    expected = "sinitsabar"
    if raw.endswith("2671/"):
        with pytest.raises(ValueError):  # ссылка на пост, а не на канал
            normalize_channel(raw)
        return
    assert normalize_channel(raw) == expected


@pytest.mark.parametrize("raw", ["", "ab", "https://t.me/+AbCdEf123", "канал", "sinitsa bar"])
def test_normalize_channel_rejects(raw):
    with pytest.raises(ValueError):
        normalize_channel(raw)


def test_settings_validation():
    assert TGSettings.from_overrides({"max_pages": 3}).max_pages == 3
    for bad in ({"page_size": 10}, {"recheck_days": 0}, {"page_delay_s": -1}):
        with pytest.raises(ValueError):
            TGSettings.from_overrides(bad)


# --- Загрузка через сервер ---


def test_curl_command_is_quoted():
    command = fetch.curl_command("https://t.me/s/sinitsabar?before=2665")
    assert command.endswith("'https://t.me/s/sinitsabar?before=2665'")
    assert "--fail" in command and "--max-time 30" in command


class FakeStream:
    def __init__(self, data: bytes, status: int = 0):
        self.data, self.channel = data, self
        self.status = status

    def read(self):
        return self.data

    def recv_exit_status(self):
        return self.status


class FakeSSH:
    def __init__(self, body: bytes, status: int = 0, err: bytes = b""):
        self.body, self.status, self.err = body, status, err
        self.commands = []

    def exec_command(self, command, timeout):
        self.commands.append(command)
        return None, FakeStream(self.body, self.status), FakeStream(self.err)

    def close(self):
        pass


def test_ssh_fetcher_runs_curl_on_server():
    client = FakeSSH("<html>Синица</html>".encode())
    assert fetch.SSHFetcher(client).get("https://t.me/s/sinitsabar") == "<html>Синица</html>"
    assert client.commands[0].startswith("curl ")


def test_ssh_fetcher_reports_curl_errors():
    client = FakeSSH(b"", status=28, err=b"curl: (28) Operation timed out")
    with pytest.raises(fetch.FetchError, match="кодом 28"):
        fetch.SSHFetcher(client).get("https://t.me/s/sinitsabar")


# --- Раздел tg: в yaml города ---

CITY_YAML = """
city: {slug: lipetsk, name: Липецк, timezone: Europe/Moscow}
vk:
  - handle: club1
tg:
  - handle: https://t.me/sinitsabar
    note: Бар «Синица»
    settings: {max_pages: 3}
"""


def test_city_config_with_telegram(tmp_path):
    path = tmp_path / "lipetsk.yaml"
    path.write_text(CITY_YAML, encoding="utf-8")
    cfg = load_city_config(path)
    assert [s.handle for s in cfg.vk] == ["club1"]
    assert [(s.handle, s.settings) for s in cfg.tg] == [("sinitsabar", {"max_pages": 3})]


@pytest.mark.parametrize(
    "tg",
    [
        "  - handle: https://t.me/+AbCdEf123",  # закрытый канал по ссылке-приглашению
        "  - handle: sinitsabar\n  - handle: '@sinitsabar'",  # дважды
        "  - handle: sinitsabar\n    settings: {page_size: 50}",  # настройка от VK
        "  - handle: sinitsabar\n    venue: sinitsa",  # площадки нет в справочнике
    ],
)
def test_bad_telegram_sources_rejected(tmp_path, tg):
    path = tmp_path / "lipetsk.yaml"
    path.write_text(CITY_YAML.split("tg:")[0] + "tg:\n" + tg + "\n", encoding="utf-8")
    with pytest.raises(SourcesConfigError):
        load_city_config(path)
