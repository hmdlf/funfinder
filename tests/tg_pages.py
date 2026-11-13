"""Страницы t.me/s/<канал> для тестов: та же разметка, что у настоящей веб-версии.

Структура снята с живой страницы канала (ноябрь 2026): вложенный
div.js-message_text, цитата ответа в a.tgme_widget_message_reply, фото альбома
в a.tgme_widget_message_photo_wrap, ссылка на более старые посты в <link rel="prev">.
Тексты постов придуманы.
"""

from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urlparse

PAGE_SIZE = 20


@dataclass
class Post:
    post_id: int
    published_at: datetime
    text_html: str = ""
    reply_html: str | None = None
    photos: int = 0
    videos: int = 0
    service: bool = False


def message_html(channel: str, p: Post) -> str:
    reply = ""
    if p.reply_html is not None:
        reply = (
            f'<a class="tgme_widget_message_reply user-color-10" href="https://t.me/{channel}/{p.post_id - 1}">'
            '<div class="tgme_widget_message_author accent_color">'
            '<span class="tgme_widget_message_author_name" dir="auto">Канал</span></div>'
            f'<div class="tgme_widget_message_text js-message_reply_text" dir="auto">{p.reply_html}</div></a>'
        )
    media = "".join(
        f'<a class="tgme_widget_message_photo_wrap grouped_media_wrap blured js-message_photo" '
        f"style=\"background-image:url('https://cdn.example/{i}.jpg')\" "
        f'href="https://t.me/{channel}/{p.post_id + i}?single"></a>'
        for i in range(p.photos)
    ) + "".join(
        '<a class="tgme_widget_message_video_player grouped_media_wrap blured js-message_video_player"></a>'
        for _ in range(p.videos)
    )
    text = ""
    if p.text_html:
        text = (
            '<div class="tgme_widget_message_text js-message_text" dir="auto">'
            f'<div class="tgme_widget_message_text js-message_text" dir="auto">{p.text_html}</div></div>'
        )
    classes = "tgme_widget_message text_not_supported_wrap user-color-10 js-widget_message"
    if p.service:
        classes += " service_message"
    return (
        '<div class="tgme_widget_message_wrap js-widget_message_wrap">'
        f'<div class="{classes}" data-post="{channel}/{p.post_id}">'
        '<div class="tgme_widget_message_bubble">'
        f"{reply}{media}{text}"
        '<div class="tgme_widget_message_footer compact js-message_footer">'
        '<div class="tgme_widget_message_info short js-message_info">'
        '<span class="tgme_widget_message_views">518</span><span class="tgme_widget_message_meta">'
        f'<a class="tgme_widget_message_date" href="https://t.me/{channel}/{p.post_id}">'
        f'<time datetime="{p.published_at.isoformat()}" class="time">14:46</time></a></span>'
        "</div></div></div></div></div>"
    )


def channel_page(channel: str, posts: list[Post], before: int | None = None) -> str:
    prev = f'<link rel="prev" href="/s/{channel}?before={before}">' if before else ""
    body = "".join(message_html(channel, p) for p in posts)
    return (
        f"<!DOCTYPE html><html><head>{prev}"
        f'<link rel="canonical" href="/s/{channel}"></head><body>'
        '<div class="tgme_channel_info"><div class="tgme_channel_info_header_title">Канал</div></div>'
        f'<section class="tgme_channel_history js-message_history">{body}</section></body></html>'
    )


def missing_channel_page() -> str:
    """Что отдаёт t.me для несуществующего или закрытого канала: страницу без ленты."""
    return '<html><body><div class="tgme_page"><div class="tgme_page_title">Telegram</div></div></body></html>'


@dataclass
class FakeChannel:
    """Канал, который отдаёт страницы как t.me: по PAGE_SIZE постов, ?before=<id> — старее."""

    name: str
    posts: list[Post] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)

    def get(self, url: str) -> str:
        self.requests.append(url)
        before = parse_qs(urlparse(url).query).get("before")
        older = [p for p in self.posts if not before or p.post_id < int(before[0])]
        page = sorted(older, key=lambda p: p.post_id)[-PAGE_SIZE:]
        has_older = len(older) > len(page)
        return channel_page(self.name, page, before=page[0].post_id if page and has_older else None)

    def close(self) -> None:
        pass
