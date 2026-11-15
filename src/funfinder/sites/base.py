"""Общее для разбора сайтов площадок: показ, загрузка страниц с учётом robots.txt, чистка названий.

Каждый сайт — свой модуль с функцией collect(get, today) -> Listing: какие
страницы загрузить и как их разобрать. get(url) возвращает байты: кодировку
(у театра Толстого — windows-1251) определяет BeautifulSoup по <meta charset>.
"""

import re
from dataclasses import dataclass, field
from datetime import date, time
from urllib.parse import urlsplit

import requests

USER_AGENT = "Mozilla/5.0 (compatible; FunFinder/1.0; +https://matingu.com)"
ROBOTS_AGENT = "FunFinder"
TIMEOUT_S = 30

MONTHS_NOMINATIVE = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]  # fmt: skip
MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]  # fmt: skip


class FetchError(Exception):
    """Сеть, таймаут, 5xx: Airflow повторит задачу."""


class RobotsDisallowed(Exception):
    """robots.txt сайта запрещает эту страницу. Не обходим: убираем страницу из разбора."""


class SiteFormatError(Exception):
    """На странице не нашлось того, за что держится разбор: сайт переделали, нужна правка кода."""


@dataclass(frozen=True)
class SiteEvent:
    key: str  # уникален внутри сайта: «834@2026-11-14@11:00»
    title: str
    day: date
    at: time | None
    url: str
    category: str | None = None  # код из meta.categories, если сайт его указывает; None — категория сайта из yaml


@dataclass
class Listing:
    events: list[SiteEvent] = field(default_factory=list)
    months: set[tuple[int, int]] = field(default_factory=set)  # какие месяцы афиша показывает целиком


def clean_title(raw: str) -> str:
    """«"Танцы на закате"  16+» → «Танцы на закате»: без возрастной метки, кавычки-ёлочки, один пробел."""
    title = re.sub(r"\s+", " ", raw).strip()
    title = re.sub(r"\s*\(?\b\d{1,2}\s?\+\)?\s*$", "", title)  # 6+, (16+)
    title = re.sub(r'"([^"]+)"', r"«\1»", title)
    return title.strip(" ,")


def robots_rules(text: str, agent: str = ROBOTS_AGENT) -> list[tuple[bool, str]]:
    """Правила из robots.txt для нашего робота: [(разрешено?, шаблон пути)].

    Своя группа (User-agent: FunFinder), если есть, иначе общая (User-agent: *).
    Стандартный urllib.robotparser не понимает «*» внутри пути, а у сайтов на
    MODX типичное правило — «Disallow: /*?», поэтому разбираем сами.
    """
    groups: list[tuple[set[str], list[tuple[bool, str]]]] = []
    agents: set[str] = set()
    rules: list[tuple[bool, str]] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules:  # новая группа начинается после правил предыдущей
                groups.append((agents, rules))
                agents, rules = set(), []
            agents.add(value.lower())
        elif key in ("allow", "disallow") and agents:
            if value:  # пустой Disallow ничего не запрещает
                rules.append((key == "allow", value))
    if agents:
        groups.append((agents, rules))
    own = [r for a, r in groups if agent.lower() in a]
    return [rule for group in (own or [r for a, r in groups if "*" in a]) for rule in group]


def robots_allows(rules: list[tuple[bool, str]], url: str) -> bool:
    """Самое длинное подходящее правило побеждает; при равной длине — разрешающее (как у Google и Яндекса)."""
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    best: tuple[int, bool] | None = None
    for allow, pattern in rules:
        regex = re.escape(pattern).replace(r"\*", ".*")
        if regex.endswith(r"\$"):
            regex = regex[:-2] + "$"
        if re.match(regex, path):
            candidate = (len(pattern), allow)
            if best is None or candidate > best:
                best = candidate
    return best is None or best[1]


class SiteFetcher:
    """Загрузка страниц сайта напрямую, с учётом его robots.txt."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._robots: dict[str, list[tuple[bool, str]]] = {}

    def _rules_for(self, url: str) -> list[tuple[bool, str]]:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            try:
                resp = self.session.get(f"{origin}/robots.txt", timeout=TIMEOUT_S)
            except requests.RequestException as exc:
                raise FetchError(f"{origin}/robots.txt: {exc}") from exc
            if resp.status_code >= 500:
                raise FetchError(f"{origin}/robots.txt: HTTP {resp.status_code}")
            # Нет файла (4xx) — ограничений нет.
            self._robots[origin] = robots_rules(resp.text) if resp.status_code == 200 else []
        return self._robots[origin]

    def get(self, url: str) -> bytes:
        if not robots_allows(self._rules_for(url), url):
            raise RobotsDisallowed(f"{url} закрыта в robots.txt сайта")
        try:
            resp = self.session.get(url, timeout=TIMEOUT_S)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise FetchError(f"{url}: {exc}") from exc
        return resp.content

    def close(self) -> None:
        self.session.close()
