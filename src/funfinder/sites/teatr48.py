"""Драматический театр «На Соколе»: https://www.teatr48.ru/

Страница «Афиша» у театра пустая (её рисует виджет кассы), зато на главной
есть календарь текущего месяца: ячейка td.cell[data-fulldate="2026-11-14"],
в ней по блоку на показ — время в span.eventdate («14 ноя 2026 18:00») и
ссылка на страницу спектакля.

Календарь следующего месяца сайт отдаёт по адресу с параметрами
(/?action=refreshCalendar&month=12&year=2026), а robots.txt закрывает адреса
с «?». Такие страницы не загружаем: следующий месяц появится в разборе, когда
наступит, а до тех пор его покрывают анонсы из пабликов.
"""

import logging
import re
from datetime import date, time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from funfinder.sites.base import MONTHS_NOMINATIVE, Listing, RobotsDisallowed, SiteEvent, SiteFormatError, clean_title

log = logging.getLogger(__name__)

URL = "https://www.teatr48.ru/"
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
_MONTH_RE = re.compile(r"([а-я]+)\s+(\d{4})")


def month_url(year: int, month: int) -> str:
    return f"{URL}?action=refreshCalendar&month={month}&year={year}"


def parse(html: bytes, url: str = URL) -> Listing:
    soup = BeautifulSoup(html, "html.parser")
    calendar = soup.select_one("table.calendar")
    if calendar is None:
        raise SiteFormatError("На странице нет календаря афиши (table.calendar)")
    base = soup.select_one("base[href]")
    base_url = urljoin(url, base["href"]) if base else url

    listing = Listing()
    header = calendar.select_one("td.month")
    match = _MONTH_RE.search(header.get_text(" ", strip=True).lower()) if header else None
    if match and match.group(1) in MONTHS_NOMINATIVE:
        listing.months.add((int(match.group(2)), MONTHS_NOMINATIVE.index(match.group(1)) + 1))

    found: dict[str, SiteEvent] = {}
    for cell in calendar.select("td.cell[data-fulldate]"):
        y, m, d = (int(x) for x in cell["data-fulldate"].split("-"))
        day = date(y, m, d)
        for block in cell.select("div.event > div"):
            link = block.select_one("span.link a[href]")
            when = block.select_one("span.eventdate")
            if link is None:
                continue
            t = _TIME_RE.search(when.get_text(" ", strip=True)) if when else None
            at = time(int(t.group(1)), int(t.group(2))) if t else None
            show_url = urljoin(base_url, link["href"])
            slug = link["href"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".html")
            key = f"{slug}@{day.isoformat()}@{at:%H:%M}" if at else f"{slug}@{day.isoformat()}"
            found[key] = SiteEvent(key=key, title=clean_title(link.get_text()), day=day, at=at, url=show_url)

    listing.events = sorted(found.values(), key=lambda e: (e.day, e.at or time(0), e.key))
    return listing


def collect(get, today: date) -> Listing:
    """Календарь на главной (текущий месяц) и, если robots.txt разрешит, следующий месяц."""
    listing = parse(get(URL))
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    try:
        extra = parse(get(month_url(year, month)))
    except RobotsDisallowed as exc:
        log.info("Следующий месяц не загружаем: %s", exc)
        return listing
    events = {e.key: e for e in listing.events + extra.events}
    listing.events = sorted(events.values(), key=lambda e: (e.day, e.at or time(0), e.key))
    listing.months |= extra.months
    return listing
