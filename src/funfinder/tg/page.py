"""Разбор веб-версии публичного канала: https://t.me/s/<канал>.

Страница отдаёт около 20 последних постов, более старые — по ссылке
?before=<id> (её адрес есть в <link rel="prev">). Альбом из нескольких фото
— это один пост с id первого фото; поэтому id идут не подряд.

Разметка не документирована, поэтому разбор держится за немногие стабильные
классы и падает громко, если пропало главное (дата, id поста).
"""

import re
from dataclasses import dataclass
from datetime import datetime

from bs4 import BeautifulSoup, Tag

_BEFORE_RE = re.compile(r"[?&]before=(\d+)")


class ChannelUnavailable(Exception):
    """Канала нет или у него нет веб-версии (закрытый канал): повтор не поможет."""


class PageFormatError(Exception):
    """Разметка t.me поменялась: пост без даты или id. Нужна правка разбора."""


@dataclass(frozen=True)
class TGPost:
    post_id: int
    published_at: datetime
    text: str
    reply_text: str  # цитата поста, на который это ответ
    media_count: int
    url: str


@dataclass(frozen=True)
class Page:
    posts: list[TGPost]
    before: int | None  # id для следующей, более старой страницы; None — старше постов нет


def _text(node: Tag | None) -> str:
    """Текст с переносами строк вместо <br>. Эмодзи остаются: они лежат внутри <b>."""
    if node is None:
        return ""
    for br in node.find_all("br"):
        br.replace_with("\n")
    text = node.get_text()
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _own_text_node(message: Tag) -> Tag | None:
    """Текст самого поста. t.me вкладывает div.js-message_text в такой же div —
    берём внешний. Текст цитаты ответа помечен иначе (js-message_reply_text)."""
    for node in message.select("div.js-message_text"):
        if node.find_parent("div", class_="js-message_text") is None:
            return node
    return None


def parse_page(html: str, channel: str) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one(".tgme_channel_info") is None:
        # Несуществующий или закрытый канал: t.me перенаправляет на страницу без постов.
        raise ChannelUnavailable(f"У канала {channel} нет открытой веб-версии t.me/s/{channel}")

    posts = []
    for message in soup.select("div.tgme_widget_message[data-post]"):
        if "service_message" in message.get("class", []):
            continue  # «канал создан», «сообщение закреплено»
        data_post = message["data-post"]  # sinitsabar/2671
        try:
            post_id = int(data_post.rsplit("/", 1)[1])
        except (IndexError, ValueError) as exc:
            raise PageFormatError(f"Непонятный data-post: {data_post!r}") from exc

        time_tag = message.select_one("a.tgme_widget_message_date time[datetime]")
        if time_tag is None:
            raise PageFormatError(f"У поста {data_post} нет даты")
        link = message.select_one("a.tgme_widget_message_date")

        posts.append(
            TGPost(
                post_id=post_id,
                published_at=datetime.fromisoformat(time_tag["datetime"]),
                text=_text(_own_text_node(message)),
                reply_text=_text(message.select_one(".tgme_widget_message_reply .js-message_reply_text")),
                media_count=len(message.select("a.tgme_widget_message_photo_wrap, .tgme_widget_message_video_player")),
                url=link.get("href") or f"https://t.me/{channel}/{post_id}",
            )
        )

    prev = soup.select_one('link[rel="prev"]')
    match = _BEFORE_RE.search(prev["href"]) if prev and prev.get("href") else None
    return Page(posts=posts, before=int(match.group(1)) if match else None)
