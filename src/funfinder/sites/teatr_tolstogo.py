"""Театр драмы им. Толстого: https://www.teatr-tolstogo.ru/afisha/time/

Одна страница на всю афишу, обычно на текущий и следующий месяц. Блок
div.month на месяц («Спектакли на ноябрь, 2026»), в нём div.spect на спектакль:
ссылка на его страницу и показы. У каждого показа есть машинный ключ кнопки
«купить» — data-hwm-event-id="834@2026-11-14@11:00": из него берём дату и время.
Если кнопки нет (билетов не продают), дату и время читаем из текста «14 ноября, 11:00».
"""

import re
from datetime import date, time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from funfinder.sites.base import MONTHS_GENITIVE, MONTHS_NOMINATIVE, Listing, SiteEvent, SiteFormatError, clean_title

URL = "https://www.teatr-tolstogo.ru/afisha/time/"

_KEY_RE = re.compile(r"^(\d+)@(\d{4})-(\d{1,2})-(\d{1,2})@(\d{1,2}):(\d{2})$")
_TEXT_RE = re.compile(r"(\d{1,2})\s+([а-я]+),?\s+(\d{1,2}):(\d{2})")
_MONTH_TITLE_RE = re.compile(r"([а-я]+),\s*(\d{4})")


def parse(html: bytes, url: str = URL) -> Listing:
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select("div.month[data-month]")
    if not blocks:
        raise SiteFormatError("На странице афиши нет блоков месяцев (div.month)")

    listing = Listing()
    found: dict[str, SiteEvent] = {}
    for block in blocks:
        match = _MONTH_TITLE_RE.search(block.select_one(".mtitle").get_text(" ", strip=True).lower())
        if not match or match.group(1) not in MONTHS_NOMINATIVE:
            raise SiteFormatError(f"Непонятный заголовок месяца: {block.select_one('.mtitle').get_text(strip=True)!r}")
        year, month = int(match.group(2)), MONTHS_NOMINATIVE.index(match.group(1)) + 1
        listing.months.add((year, month))

        for spect in block.select("div.spect"):
            link = spect.select_one("h3 a[href]")
            if link is None:
                continue
            spect_url = urljoin(url, link["href"])
            spect_id = re.search(r"(\d+)\.html", link["href"])
            title = clean_title(link.get_text())

            shows: list[tuple[date, time]] = []
            for tag in spect.select("[data-hwm-event-id]"):
                m = _KEY_RE.match(tag["data-hwm-event-id"].strip())
                if m:
                    _, y, mo, d, hh, mm = (int(x) for x in m.groups())
                    shows.append((date(y, mo, d), time(hh, mm)))
            if not shows:  # кнопок покупки нет — читаем текст дат
                for line in spect.select(".date .right div"):
                    m = _TEXT_RE.search(line.get_text(" ", strip=True).lower())
                    if m and m.group(2) in MONTHS_GENITIVE:
                        day = date(year, MONTHS_GENITIVE.index(m.group(2)) + 1, int(m.group(1)))
                        shows.append((day, time(int(m.group(3)), int(m.group(4)))))

            for day, at in shows:
                key = f"{spect_id.group(1) if spect_id else spect_url}@{day.isoformat()}@{at:%H:%M}"
                found[key] = SiteEvent(key=key, title=title, day=day, at=at, url=spect_url)

    listing.events = sorted(found.values(), key=lambda e: (e.day, e.at or time(0), e.key))
    return listing


def collect(get, today: date) -> Listing:
    """get(url) -> bytes. Вся афиша — на одной странице."""
    return parse(get(URL))
