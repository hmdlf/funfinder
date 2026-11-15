"""ОЦКНТ — Областной центр культуры, народного творчества и кино: https://ocknt.ru/afisha/

Сайт на Битриксе. Афиша — карточки по 12 на страницу, страницы ?PAGEN_1=2…N,
по возрастанию даты. В карточке (div.container-cards-affiche):
- название — h3.card-title, ссылка — на страницу события /afisha/<slug>/;
- дата и время — div.date-active-from. Сервер кладёт туда Unix-время
  (1794240000), а «09 ноября 2026, 19:00» из него рисует скрипт в браузере —
  по московскому времени. Поэтому страница, сохранённая из браузера, и ответ
  сервера различаются; разбираем оба вида;
- возраст и тег категории (Театр, Концерт, Кино, Балет, Мастер-класс) — рядом
  со значками user.svg и tag.svg. Тег → наша категория (CATEGORIES); неизвестный
  тег — категория сайта из yaml.

Чего на сайте нет: мероприятий сторонних организаторов в аренду. Концерт Маши
Распутиной 16.11 в зале ОЦКНТ на сайте не значится — такие придут из пабликов.

Страницы листаем, пока они есть, но не больше MAX_PAGES. Если robots.txt
запретит пагинацию (у Битрикса её часто закрывают), остаётся первая страница.
Целиком показанными (для отметки снятых показов) считаются месяцы раньше месяца
последнего прочитанного показа; если дочитали все страницы — все месяцы.
"""

import logging
import re
from datetime import date, datetime, time
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from funfinder.sites.base import MONTHS_GENITIVE, Listing, RobotsDisallowed, SiteEvent, SiteFormatError, clean_title

log = logging.getLogger(__name__)

URL = "https://ocknt.ru/afisha/"
MAX_PAGES = 10
# Время на сайте — местное, Липецк. Проверено: 1794240000 сайт показывает как «09 ноября 2026, 19:00».
TZ = ZoneInfo("Europe/Moscow")

CATEGORIES = {
    "театр": "theatre",
    "балет": "theatre",
    "концерт": "concert",
    "кино": "cinema",
    "мастер-класс": "workshop",
    "выставка": "exhibition",
}

_DATE_RE = re.compile(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})(?:\s*,\s*(\d{1,2}):(\d{2}))?")
_PAGE_RE = re.compile(r"PAGEN_1=(\d+)")


def page_url(n: int) -> str:
    return URL if n == 1 else f"{URL}?PAGEN_1={n}"


def _from_timestamp(raw: str) -> tuple[date, time | None] | None:
    """Unix-время из HTML сервера → местные дата и время. Полночь — время не указано."""
    stamp = int(raw)
    if stamp > 10**11:  # миллисекунды
        stamp //= 1000
    try:
        local = datetime.fromtimestamp(stamp, TZ)
    except (OverflowError, OSError, ValueError):
        return None
    if not 2000 <= local.year <= 2100:
        return None
    at = local.time().replace(second=0, microsecond=0)
    return local.date(), (at if at != time(0) else None)


def _when(text: str) -> tuple[date, time | None] | None:
    """«09 декабря 2026, 19:00» (страница в браузере) или 1794240000 (ответ сервера)."""
    if text.strip().isdigit():
        return _from_timestamp(text.strip())
    m = _DATE_RE.search(text.lower())
    if not m or m.group(2) not in MONTHS_GENITIVE:
        return None
    day = date(int(m.group(3)), MONTHS_GENITIVE.index(m.group(2)) + 1, int(m.group(1)))
    at = time(int(m.group(4)), int(m.group(5))) if m.group(4) else None
    return day, at


def parse(html: bytes, url: str = URL) -> tuple[list[SiteEvent], int]:
    """Одна страница афиши → показы и номер последней страницы."""
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.container-cards-affiche")
    if not cards:
        raise SiteFormatError("На странице афиши нет карточек (div.container-cards-affiche)")

    events = []
    for card in cards:
        link = card.select_one("a[href] h3.card-title")
        when = card.select_one(".date-active-from")
        if link is None or when is None:
            continue
        parsed = _when(when.get_text(" ", strip=True))
        if parsed is None:
            log.warning("ОЦКНТ: непонятная дата %r у %r", when.get_text(strip=True), link.get_text(strip=True))
            continue
        day, at = parsed
        event_url = urljoin(url, link.find_parent("a")["href"])
        slug = urlsplit(event_url).path.rstrip("/").rsplit("/", 1)[-1]
        tag = next(
            (img.parent.get_text(" ", strip=True).lower() for img in card.select("img[src$='tag.svg']")),
            "",
        )
        events.append(
            SiteEvent(
                key=f"{slug}@{day.isoformat()}@{at:%H:%M}" if at else f"{slug}@{day.isoformat()}",
                title=clean_title(link.get_text(" ")),
                day=day,
                at=at,
                url=event_url,
                category=CATEGORIES.get(tag),
            )
        )
    if not events:
        raise SiteFormatError("Карточки есть, но ни у одной не разобралась дата")

    pages = [int(n) for a in soup.select("a[href*='PAGEN_1=']") for n in _PAGE_RE.findall(a["href"])]
    return events, max(pages, default=1)


def collect(get, today: date) -> Listing:
    """get(url) -> bytes. Листает страницы афиши, пока они есть и robots.txt их разрешает."""
    events, last = parse(get(page_url(1)))
    complete = True
    page = 1
    while page < min(last, MAX_PAGES):
        page += 1
        try:
            more, last_seen = parse(get(page_url(page)), page_url(page))
        except RobotsDisallowed:
            log.info("ОЦКНТ: robots.txt закрывает страницу %d, берём только прочитанные", page)
            complete = False
            break
        events.extend(more)
        last = max(last, last_seen)
    if last > MAX_PAGES:
        complete = False

    found = {e.key: e for e in events}
    listing = Listing(events=sorted(found.values(), key=lambda e: (e.day, e.at or time(0), e.key)))
    months = {(e.day.year, e.day.month) for e in listing.events}
    if not complete and listing.events:
        tail = listing.events[-1].day
        months = {m for m in months if m < (tail.year, tail.month)}  # последний месяц мог дочитаться не весь
    listing.months = months
    return listing
