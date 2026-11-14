"""build_site: catalog.events_public → статические HTML-страницы.

На выходе папка, которую nginx отдаёт как есть:

    index.html                  переход на страницу города (пока город один)
    lipetsk/index.html          все будущие события города
    lipetsk/<категория>/        события одной категории
    lipetsk/events/<адрес>/     страница события; адреса постоянные (funfinder.site.pages)
    assets/                     стили, скрипт, шрифты
    sitemap.xml, robots.txt, 404.html

«Сегодня», «завтра» и «выходные» считает скрипт в браузере по текущей дате
в часовом поясе города. Поэтому страницы не врут, даже если их давно не
пересобирали: прошедшие дни просто скрываются.
"""

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from funfinder.site import pages as event_pages
from funfinder.site.pages import PageRow

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
SELECT event_id, title, event_date, event_time, venue, venue_address, venue_slug, price, category, status,
       source_urls
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
    url: str  # первый пост-источник
    address: str | None = None  # из справочника площадок
    venue_slug: str | None = None
    urls: list[str] = field(default_factory=list)  # все посты-источники
    slug: str | None = None  # адрес страницы события, выдаёт pages.assign

    @property
    def status_label(self) -> str | None:
        return STATUS_LABELS.get(self.status)

    @property
    def sources(self) -> list[dict]:
        """Ссылки на посты с понятной подписью: «VK», «Telegram»; несколько из одного места — «VK 1», «VK 2»."""
        urls = self.urls or [self.url]
        labels = [source_label(u) for u in urls]
        seen: dict[str, int] = {}
        result = []
        for url, label in zip(urls, labels, strict=True):
            if labels.count(label) > 1:
                seen[label] = seen.get(label, 0) + 1
                label = f"{label} {seen[label]}"
            result.append({"url": url, "label": label})
        return result


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


def source_label(url: str) -> str:
    if "t.me/" in url:
        return "Telegram"
    if "vk.com/" in url or "vk.ru/" in url:
        return "VK"
    return url.split("://", 1)[-1].split("/", 1)[0]


def human_date(day: date, today: date | None = None) -> str:
    """«четверг, 12 ноября»; год — только если не текущий."""
    text = f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS_GENITIVE[day.month - 1]}"
    return text + (f" {day.year}" if today and day.year != today.year else "")


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


def json_ld(events: list[Event], *, city: str, tz: ZoneInfo, pages_url: str | None = None) -> str:
    """Разметка schema.org/Event: по ней поисковики показывают события в выдаче.

    pages_url — адрес папки страниц событий (https://matingu.com/lipetsk/events/): тогда
    у события в разметке ссылка на его страницу, иначе — на пост.
    """
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
                "address": {
                    "@type": "PostalAddress",
                    **({"streetAddress": e.address} if e.address else {}),
                    "addressLocality": city,
                    "addressCountry": "RU",
                },
            },
            "url": f"{pages_url}{e.slug}/" if pages_url and e.slug else e.url,
        }
        items.append(item)
    data = {"@context": "https://schema.org", "@graph": items}
    # Текст из постов попадает внутрь <script>: экранируем < > &, чтобы никакое
    # название не могло закрыть тег или превратиться в разметку. JSON читает < как <.
    text = json.dumps(data, ensure_ascii=False)
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def glue_abbreviations(address: str | None) -> str | None:
    """«ул. Фрунзе» не разрывается на «ул.» в конце строки и «Фрунзе» в начале следующей."""
    return re.sub(r"\b(ул|пл|пр|д|стр)\. ", "\\1.\u00a0", address) if address else address


def short_date(day: date) -> str:
    """«сб, 14 ноября» — для коротких списков."""
    return f"{WEEKDAYS_SHORT[day.weekday()]}, {day.day} {MONTHS_GENITIVE[day.month - 1]}"


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["short_date"] = short_date
    return env


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
            rows = cur.fetchall()
            events[city["slug"]] = [
                Event(
                    event_id=event_id,
                    title=title,
                    day=day,
                    at=at,
                    venue=venue,
                    address=glue_abbreviations(address),
                    venue_slug=venue_slug,
                    price=price,
                    category=category,
                    category_name=categories.get(category, category),
                    status=status,
                    url=urls[0],
                    urls=list(urls),
                )
                for event_id, title, day, at, venue, address, venue_slug, price, category, status, urls in rows
            ]
    return cities, categories, events


def _city_today(city: dict, now: datetime | None) -> date:
    tz = ZoneInfo(city["timezone"])
    return (now or datetime.now(tz)).astimezone(tz).date()


def render(
    out: Path,
    cities: list[dict],
    categories: dict[str, str],
    events: dict[str, list[Event]],
    *,
    site_url: str,
    contact: str | None = None,
    now: datetime | None = None,
    page_rows: dict[str, list[PageRow]] | None = None,
) -> dict:
    """Собирает сайт в папку out (она должна быть пустой или отсутствовать).

    page_rows — уже опубликованные адреса страниц событий по городам (catalog.event_pages).
    Список дополняется на месте: новые адреса, перенаправления, отметки «событие было в афише».
    Без него адреса выдаются заново.
    """
    site_url = site_url.rstrip("/")
    env = _environment()
    version = _copy_static(out)
    common = {"site_url": site_url, "contact": contact, "asset_version": version}
    moment = now or datetime.now(ZoneInfo("UTC"))
    page_rows = page_rows if page_rows is not None else {}
    pages: list[str] = []  # страницы списков
    event_paths: list[str] = []  # страницы будущих событий: тоже в sitemap
    event_page_count = 0

    for city in cities:
        tz = ZoneInfo(city["timezone"])
        today = _city_today(city, now)
        built_at = human_datetime(moment.astimezone(tz))
        city_events = events.get(city["slug"], [])
        rows = event_pages.assign(city_events, page_rows.get(city["slug"], []), now=moment)
        page_rows[city["slug"]] = rows
        present = {e.category for e in city_events}
        nav = [{"slug": slug, "name": name} for slug, name in categories.items() if slug in present]
        city_in = locative(city["name"])
        pages_url = f"{site_url}/{city['slug']}/events/"

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
                json_ld=json_ld(selected, city=city["name"], tz=tz, pages_url=pages_url),
            )
            _write(out, f"{path}index.html", html)
            pages.append(path)

        event_page_count += _render_event_pages(
            env, out, common, city, city_events, rows, nav=nav, today=today, tz=tz, pages_url=pages_url
        )
        event_paths += [f"{city['slug']}/events/{e.slug}/" for e in city_events]

    _write(out, "index.html", env.get_template("root.html").render(**common, cities=cities))
    _write(out, "404.html", env.get_template("404.html").render(**common, cities=cities))
    _write(out, "robots.txt", f"User-agent: *\nAllow: /\n\nSitemap: {site_url}/sitemap.xml\n")
    _write(
        out,
        "sitemap.xml",
        env.get_template("sitemap.xml").render(
            site_url=site_url, pages=pages + event_paths, today=moment.date().isoformat()
        ),
    )
    return {"pages": len(pages), "events": sum(len(v) for v in events.values()), "event_pages": event_page_count}


RELATED_LIMIT = 5


def _render_event_pages(env, out, common, city, city_events, rows, *, nav, today, tz, pages_url) -> int:
    """Страницы событий, «событие прошло» и перенаправления после склейки. Возвращает число страниц."""
    upcoming = [e for e in city_events if e.category not in HIDDEN_FROM_MAIN]
    by_slug = {r.slug: r for r in rows}
    count = 0

    def at_venue(venue_slug, exclude=None):
        if not venue_slug:
            return []
        return [e for e in city_events if e.venue_slug == venue_slug and e is not exclude][:RELATED_LIMIT]

    for e in city_events:
        same_day = [x for x in upcoming if x.day == e.day and x is not e and x.venue_slug != e.venue_slug]
        html = env.get_template("event.html").render(
            **common,
            page_url=f"{pages_url}{e.slug}/",
            city=city,
            city_in=locative(city["name"]),
            nav=nav,
            event=e,
            date_text=human_date(e.day, today),
            more_here=at_venue(e.venue_slug, exclude=e),
            same_day=same_day[:RELATED_LIMIT],
            json_ld=json_ld([e], city=city["name"], tz=tz, pages_url=pages_url),
        )
        _write(out, f"{city['slug']}/events/{e.slug}/index.html", html)
        count += 1

    for row in rows:
        if row.event_id is not None:
            continue  # у адреса есть событие — страница уже есть
        if row.redirect_to and row.redirect_to in by_slug:
            html = env.get_template("redirect.html").render(
                **common, target=f"{pages_url}{row.redirect_to}/", title=row.title, city=city
            )
        elif event_pages.visible_gone(row, today):
            here = at_venue(row.venue_slug)
            html = env.get_template("gone.html").render(
                **common,
                city=city,
                city_in=locative(city["name"]),
                row=row,
                past=row.event_date < today,
                date_text=human_date(row.event_date, today),
                more_here=here or upcoming[:RELATED_LIMIT],
                venue_known=bool(here),
            )
        else:
            continue
        _write(out, f"{city['slug']}/events/{row.slug}/index.html", html)
        count += 1
    return count


def run(conn, build_dir: Path, *, site_url: str, contact: str | None = None, now: datetime | None = None) -> dict:
    """Собирает сайт в build_dir/site. Сначала во временную папку, потом подменяет:
    если сборка упадёт, предыдущая версия останется целой. Адреса страниц событий
    сохраняются в catalog.event_pages только после удачной сборки."""
    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    fresh, current = build_dir / "site.new", build_dir / "site"
    shutil.rmtree(fresh, ignore_errors=True)

    cities, categories, events = load(conn)
    with conn, conn.cursor() as cur:
        page_rows = {c["slug"]: event_pages.load_rows(cur, c["city_id"], _city_today(c, now)) for c in cities}
    stats = render(fresh, cities, categories, events, site_url=site_url, contact=contact, now=now, page_rows=page_rows)

    shutil.rmtree(current, ignore_errors=True)
    fresh.rename(current)
    with conn, conn.cursor() as cur:
        for city in cities:
            event_pages.save_rows(cur, city["city_id"], page_rows[city["slug"]])
    return stats
