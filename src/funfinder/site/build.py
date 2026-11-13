"""build_site: catalog.events_public → статические HTML-страницы.

На выходе папка, которую nginx отдаёт как есть:

    index.html                  переход на страницу города (пока город один)
    lipetsk/index.html          все будущие события города
    lipetsk/<категория>/        события одной категории
    assets/                     стили, скрипт, шрифты
    sitemap.xml, robots.txt, 404.html

«Сегодня», «завтра» и «выходные» считает скрипт в браузере по текущей дате
в часовом поясе города. Поэтому страницы не врут, даже если их давно не
пересобирали: прошедшие дни просто скрываются.
"""

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

# Регулярные занятия (группы танго, кружки) забивали бы главную. У них своя страница.
HIDDEN_FROM_MAIN = {"classes"}

MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")  # для узкой колонки даты на телефоне
STATUS_LABELS = {"cancelled": "Отменено", "postponed": "Перенесено"}

SELECT_CITIES = "SELECT city_id, slug, name, timezone FROM meta.cities ORDER BY slug"
SELECT_CATEGORIES = "SELECT slug, name_ru FROM meta.categories ORDER BY sort_order, slug"
SELECT_EVENTS = """
SELECT event_id, title, event_date, event_time, venue, price, category, status, source_urls
  FROM catalog.events_public
 WHERE city = %s
 ORDER BY event_date, event_time NULLS LAST, title
"""


@dataclass
class Event:
    event_id: int
    title: str
    day: date
    at: time | None
    venue: str | None
    price: str | None
    category: str
    category_name: str
    status: str
    url: str

    @property
    def status_label(self) -> str | None:
        return STATUS_LABELS.get(self.status)


@dataclass
class Day:
    day: date
    events: list[Event] = field(default_factory=list)

    @property
    def weekday(self) -> str:
        return WEEKDAYS[self.day.weekday()]

    @property
    def weekday_short(self) -> str:
        return WEEKDAYS_SHORT[self.day.weekday()]

    @property
    def month(self) -> str:
        return MONTHS_GENITIVE[self.day.month - 1]


def locative(city: str) -> str:
    """«Липецк» → «Липецке». Для заголовков «Куда сходить в …»; покрывает частые окончания."""
    if city.endswith("ь"):
        return city[:-1] + "и"  # Казань → Казани
    if city.endswith(("а", "я")):
        return city[:-1] + "е"  # Москва → Москве
    if city.endswith(("о", "и", "ы", "у", "е")):
        return city  # несклоняемые: Сочи, Ростов-на-Дону оставим как есть
    return city + "е"


def human_datetime(moment: datetime) -> str:
    return f"{moment.day} {MONTHS_GENITIVE[moment.month - 1]} в {moment:%H:%M}"


def group_by_day(events: list[Event]) -> list[Day]:
    days: list[Day] = []
    for event in events:
        if not days or days[-1].day != event.day:
            days.append(Day(event.day))
        days[-1].events.append(event)
    return days


def json_ld(events: list[Event], *, city: str, tz: ZoneInfo) -> str:
    """Разметка schema.org/Event: по ней поисковики показывают события в выдаче."""
    items = []
    for e in events:
        start = datetime.combine(e.day, e.at, tz).isoformat() if e.at else e.day.isoformat()
        item = {
            "@type": "Event",
            "name": e.title,
            "startDate": start,
            "eventStatus": "https://schema.org/EventCancelled"
            if e.status == "cancelled"
            else "https://schema.org/EventScheduled",
            "location": {
                "@type": "Place",
                "name": e.venue or city,
                "address": {"@type": "PostalAddress", "addressLocality": city, "addressCountry": "RU"},
            },
            "url": e.url,
        }
        items.append(item)
    data = {"@context": "https://schema.org", "@graph": items}
    # Текст из постов попадает внутрь <script>: экранируем < > &, чтобы никакое
    # название не могло закрыть тег или превратиться в разметку. JSON читает < как <.
    text = json.dumps(data, ensure_ascii=False)
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _copy_static(out: Path) -> str:
    """Копирует стили, скрипт и шрифты. Возвращает версию для сброса кеша браузера."""
    shutil.copytree(STATIC, out / "assets")
    digest = hashlib.sha256()
    for name in ("site.css", "site.js"):
        digest.update((STATIC / name).read_bytes())
    return digest.hexdigest()[:10]


def _write(out: Path, path: str, content: str) -> None:
    target = out / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def load(conn) -> tuple[list[dict], dict[str, str], dict[str, list[Event]]]:
    """Города, справочник категорий и события по городам."""
    with conn, conn.cursor() as cur:
        cur.execute(SELECT_CITIES)
        cities = [{"city_id": c, "slug": s, "name": n, "timezone": tz} for c, s, n, tz in cur.fetchall()]
        cur.execute(SELECT_CATEGORIES)
        categories = dict(cur.fetchall())

        events: dict[str, list[Event]] = {}
        for city in cities:
            cur.execute(SELECT_EVENTS, (city["slug"],))
            events[city["slug"]] = [
                Event(
                    event_id=event_id,
                    title=title,
                    day=day,
                    at=at,
                    venue=venue,
                    price=price,
                    category=category,
                    category_name=categories.get(category, category),
                    status=status,
                    url=urls[0],
                )
                for event_id, title, day, at, venue, price, category, status, urls in cur.fetchall()
            ]
    return cities, categories, events


def render(
    out: Path,
    cities: list[dict],
    categories: dict[str, str],
    events: dict[str, list[Event]],
    *,
    site_url: str,
    contact: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Собирает сайт в папку out (она должна быть пустой или отсутствовать)."""
    site_url = site_url.rstrip("/")
    env = _environment()
    version = _copy_static(out)
    common = {"site_url": site_url, "contact": contact, "asset_version": version}
    pages: list[str] = []

    for city in cities:
        tz = ZoneInfo(city["timezone"])
        built_at = human_datetime((now or datetime.now(tz)).astimezone(tz))
        city_events = events.get(city["slug"], [])
        present = {e.category for e in city_events}
        nav = [{"slug": slug, "name": name} for slug, name in categories.items() if slug in present]
        city_in = locative(city["name"])

        variants = [(None, [e for e in city_events if e.category not in HIDDEN_FROM_MAIN])]
        variants += [(c["slug"], [e for e in city_events if e.category == c["slug"]]) for c in nav]

        for category, selected in variants:
            path = f"{city['slug']}/" + (f"{category}/" if category else "")
            if category is None:
                h1 = f"Куда сходить в {city_in}"
                title = f"Куда сходить в {city_in}: афиша на сегодня и выходные"
            elif category == "other":
                h1 = title = f"Другие события в {city_in}"
            else:
                h1 = title = f"{categories[category]} в {city_in}"
            html = env.get_template("list.html").render(
                **common,
                page_url=f"{site_url}/{path}",
                title=title,
                h1=h1,
                city=city,
                nav=nav,
                current=category,
                days=group_by_day(selected),
                count=len(selected),
                built_at=built_at,
                json_ld=json_ld(selected, city=city["name"], tz=tz),
            )
            _write(out, f"{path}index.html", html)
            pages.append(path)

    _write(out, "index.html", env.get_template("root.html").render(**common, cities=cities))
    _write(out, "404.html", env.get_template("404.html").render(**common, cities=cities))
    _write(out, "robots.txt", f"User-agent: *\nAllow: /\n\nSitemap: {site_url}/sitemap.xml\n")
    today = (now or datetime.now(ZoneInfo("UTC"))).date().isoformat()
    _write(out, "sitemap.xml", env.get_template("sitemap.xml").render(site_url=site_url, pages=pages, today=today))

    return {"pages": len(pages), "events": sum(len(v) for v in events.values())}


def run(conn, build_dir: Path, *, site_url: str, contact: str | None = None, now: datetime | None = None) -> dict:
    """Собирает сайт в build_dir/site. Сначала во временную папку, потом подменяет:
    если сборка упадёт, предыдущая версия останется целой."""
    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    fresh, current = build_dir / "site.new", build_dir / "site"
    shutil.rmtree(fresh, ignore_errors=True)

    cities, categories, events = load(conn)
    stats = render(fresh, cities, categories, events, site_url=site_url, contact=contact, now=now)

    shutil.rmtree(current, ignore_errors=True)
    fresh.rename(current)
    return stats
