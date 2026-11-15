"""Подборка на один день: какие события в неё попадают и как она выглядит.

Выбор. События дня из витрины catalog.events_public — те же, что на сайте.
Отменённые и перенесённые в подборку не идут. Категории из exclude_categories
не включаются; по умолчанию это «Занятия и кружки»: регулярные группы забили
бы подборку, как забивали главную сайта.

Порядок. По категориям в порядке справочника (концерты, театр, выставки…),
внутри категории — по времени; события без времени — в конце категории.

Площадка — короткое имя из справочника (short в sources/venues/<город>.yaml),
если оно есть: «Буксир» вместо «Галерея современного искусства «Буксир»».

Ссылка у названия события — туда, где читателю удобнее, не уводя его с площадки:
1. пост на той же платформе, что и подборка: в Telegram-подборке — пост
   Telegram-канала, откуда событие пришло (если таких постов несколько —
   самый свежий: в нём актуальное время); в VK — пост VK;
2. иначе — страница события на сайте, если она уже опубликована: адрес
   берётся так же, как его выдаёт сборка сайта (funfinder.site.pages), но
   без записи;
3. иначе — без ссылки.

Текст (render, fmt):
- html — Telegram: название события — ссылка, разделы жирным;
- vk — VK: разметки нет, разделы ЗАГЛАВНЫМИ. Ссылка-слово в записи VK бывает
  только на страницы самого VK: [https://vk.com/wall-1_2|Название]. Поэтому
  название — ссылка, только если это пост VK; страницы сайта — без ссылки;
- plain — простой текст без ссылок на события (для логов и проверки).
Во всех форматах внизу — ссылка на афишу города. Не влезает в лимит площадки —
хвост заменяется строкой «…и ещё N событий на сайте».
"""

import html
import re
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime, time
from urllib.parse import urlparse

from funfinder.site import build, pages

TG_LIMIT = 4000  # Telegram: 4096 символов видимого текста, оставляем запас
VK_LIMIT = 15000  # VK: 16384 символа текста записи вместе с разметкой ссылок
PLAIN_LIMIT = VK_LIMIT
# Полуавтомат: текст для VK приходит сообщением Telegram, а там 4096 символов вместе с шапкой.
VK_DRAFT_LIMIT = 3600
FORMATS = ("html", "vk", "plain")
TITLE_LIMIT = 120
VENUE_LIMIT = 70
PRICE_LIMIT = 30  # «Вход 400 р.» — да, абзац про скидки — нет


@dataclass(frozen=True)
class DigestSettings:
    """Настройки подборки для места публикации: раздел settings в yaml."""

    exclude_categories: tuple[str, ...] = ("classes",)

    @classmethod
    def from_overrides(cls, overrides: dict | None) -> "DigestSettings":
        overrides = dict(overrides or {})
        known = {f.name for f in fields(cls)}
        unknown = set(overrides) - known
        if unknown:
            raise ValueError(f"неизвестные настройки {sorted(unknown)}, допустимы {sorted(known)}")
        if "exclude_categories" in overrides:
            value = overrides["exclude_categories"]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError("exclude_categories — список категорий: [classes, sport]")
            overrides["exclude_categories"] = tuple(value)
        return cls(**overrides)


@dataclass
class Item:
    title: str
    at: time | None
    venue: str | None
    price: str | None
    url: str | None  # страница события на сайте; None — страницы ещё нет


@dataclass
class Section:
    name: str  # «Концерты»
    items: list[Item] = field(default_factory=list)


@dataclass
class Digest:
    city_name: str
    day: date
    city_url: str  # https://matingu.com/lipetsk/
    sections: list[Section] = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum(len(s.items) for s in self.sections)


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """plural(5, ("событие", "события", "событий")) → «событий»."""
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


EVENTS = ("событие", "события", "событий")


def _cut(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def select(
    events: list[build.Event],
    day: date,
    categories: dict[str, str],
    settings: DigestSettings,
    links: dict[int, str],
    *,
    short_names: dict[str, str] | None = None,
) -> list[Section]:
    """События дня → разделы по категориям. categories — slug → название, в порядке справочника.

    links — event_id → ссылка для названия (см. choose_links), short_names — slug площадки → короткое имя.
    """
    short_names = short_names or {}
    order = {slug: i for i, slug in enumerate(categories)}
    chosen = [
        e for e in events if e.day == day and e.status == "scheduled" and e.category not in settings.exclude_categories
    ]
    chosen.sort(key=lambda e: (order.get(e.category, len(order)), e.at is None, e.at or time(), e.title))
    sections: list[Section] = []
    for e in chosen:
        name = categories.get(e.category, e.category)
        if not sections or sections[-1].name != name:
            sections.append(Section(name))
        price = e.price if e.price and len(e.price) <= PRICE_LIMIT else None
        sections[-1].items.append(
            Item(
                title=_cut(e.title, TITLE_LIMIT) or "Событие",
                at=e.at,
                venue=_cut(short_names.get(e.venue_slug or "") or e.venue, VENUE_LIMIT),
                price=_cut(price, PRICE_LIMIT),
                url=links.get(e.event_id),
            )
        )
    return sections


def page_slugs(cur, city_id: int, events: list[build.Event], today: date) -> dict[int, str]:
    """Адреса страниц событий, которые уже есть на сайте: event_id → slug.

    Тот же расчёт, что при сборке сайта (pages.assign), но без записи: новый
    адрес, который сборка ещё не выкладывала, ссылкой не станет.
    """
    rows = pages.load_rows(cur, city_id, today)
    published = {row.slug for row in rows if row.redirect_to is None}
    pages.assign(events, rows, now=datetime.now(UTC))
    return {e.event_id: e.slug for e in events if e.slug in published}


# Платформа подборки → адреса постов этой платформы.
VK_HOSTS = ("vk.com", "vk.ru", "m.vk.com")
PLATFORM_HOSTS = {"tg": ("t.me",), "vk": VK_HOSTS}

SELECT_POSTS = """
SELECT event_id, source_url, published_at
  FROM staging.events
 WHERE event_id = ANY(%s) AND quarantine_reason IS NULL
"""


def same_platform_posts(rows, platform: str) -> dict[int, str]:
    """(event_id, source_url, published_at) → event_id → самый свежий пост на платформе подборки."""
    hosts = PLATFORM_HOSTS.get(platform, ())
    best: dict[int, tuple] = {}
    for event_id, url, published_at in rows:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if host in hosts and (event_id not in best or (published_at, url) > best[event_id]):
            best[event_id] = (published_at, url)
    return {event_id: url for event_id, (_, url) in best.items()}


def choose_links(posts: dict[int, str], slugs: dict[int, str], pages_url: str) -> dict[int, str]:
    """Пост на той же платформе важнее страницы сайта: читатель остаётся там, где читает подборку."""
    links = {event_id: f"{pages_url}{slug}/" for event_id, slug in slugs.items()}
    links.update(posts)
    return links


def load(
    conn, city: dict, day: date, settings: DigestSettings, *, site_url: str, today: date, platform: str = "tg"
) -> Digest:
    """city — city_id, slug, name. Читает витрину, посты и адреса страниц, ничего не пишет."""
    _, categories, events = build.load(conn)
    day_events = [e for e in events.get(city["slug"], []) if e.day == day]
    with conn, conn.cursor() as cur:
        slugs = page_slugs(cur, city["city_id"], day_events, today)
        cur.execute(SELECT_POSTS, ([e.event_id for e in day_events],))
        posts = same_platform_posts(cur.fetchall(), platform)
        cur.execute(
            "SELECT slug, short_name FROM meta.venues WHERE city_id = %s AND short_name IS NOT NULL",
            (city["city_id"],),
        )
        short_names = dict(cur.fetchall())
    site_url = site_url.rstrip("/")
    links = choose_links(posts, slugs, f"{site_url}/{city['slug']}/events/")
    return Digest(
        city_name=city["name"],
        day=day,
        city_url=f"{site_url}/{city['slug']}/",
        sections=select(day_events, day, categories, settings, links, short_names=short_names),
    )


# --- Текст ---


def heading(d: Digest, today: date) -> str:
    """«Завтра в Липецке», «Сегодня в Липецке» или «Афиша Липецка» — смотря на какой день подборка."""
    where = build.locative(d.city_name)
    delta = (d.day - today).days
    if delta == 1:
        return f"Завтра в {where}"
    if delta == 0:
        return f"Сегодня в {where}"
    return f"Куда сходить в {where}"


def vk_link(url: str | None) -> str | None:
    """Адрес для ссылки-слова в записи VK: только страницы VK, приведённые к https://vk.com/…"""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc.lower().removeprefix("www.") not in VK_HOSTS or not parsed.path.strip("/"):
        return None
    return "https://vk.com" + parsed.path + (f"?{parsed.query}" if parsed.query else "")


def vk_safe(text: str) -> str:
    """Квадратные скобки и «|» в тексте VK — это разметка ссылок: [club1|…]. Заменяем на безопасные."""
    return text.replace("[", "(").replace("]", ")").replace("|", "/")


def _line(item: Item, *, fmt: str) -> tuple[str, str]:
    """Строка события: (что отправить, что считать в лимит). «19:00 Квиз «Кино» — Гастробар «Дача», 500 ₽».

    В Telegram лимит — по видимому тексту (теги не считаются), в VK — по всему
    тексту записи вместе с разметкой ссылок.
    """
    at = f"{item.at:%H:%M} " if item.at else ""
    tail = (f" — {item.venue}" if item.venue else "") + (f", {item.price}" if item.price else "")
    plain = f"{at}{item.title}{tail}"
    if fmt == "plain":
        return plain, plain
    if fmt == "vk":
        title, url = vk_safe(item.title), vk_link(item.url)
        sent = f"{at}{f'[{url}|{title}]' if url else title}{vk_safe(tail)}"
        return sent, sent
    title = html.escape(item.title)
    if item.url:
        title = f'<a href="{html.escape(item.url, quote=True)}">{title}</a>'
    return f"{html.escape(at)}{title}{html.escape(tail)}", plain


def render(d: Digest, *, today: date, fmt: str, limit: int) -> tuple[str, int]:
    """Текст подборки в формате fmt (html, vk, plain) и сколько событий в него поместилось.

    Строка — пара (что отправить, что считать в лимит): в HTML теги и ссылки
    не занимают места в лимите Telegram, считается только видимый текст.
    """
    if fmt not in FORMATS:
        raise ValueError(f"неизвестный формат {fmt!r}, допустимы {FORMATS}")
    as_html = fmt == "html"
    esc = html.escape if as_html else (lambda s: s)
    title, date_line = heading(d, today), build.human_date(d.day, today)
    domain = urlparse(d.city_url).netloc
    if as_html:
        head = (f"<b>{esc(title)}</b>\n{esc(date_line)}", f"{title}\n{date_line}")
        link = html.escape(d.city_url, quote=True)
        footer = (f'\n<a href="{link}">Вся афиша — {esc(domain)}</a>', f"\nВся афиша — {domain}")
    else:
        head = (f"{title}\n{date_line}",) * 2
        footer = (f"\nВся афиша: {d.city_url}",) * 2
    budget = limit - len(footer[1]) - 60  # место под подвал и строку «…и ещё N событий на сайте»

    lines, used, shown, full = [head], len(head[1]), 0, True
    for section in d.sections:
        name = (f"\n<b>{esc(section.name)}</b>", f"\n{section.name}") if as_html else (f"\n{section.name.upper()}",) * 2
        header_pending = True
        for item in section.items:
            line = _line(item, fmt=fmt)
            cost = len(line[1]) + 1 + (len(name[1]) + 1 if header_pending else 0)
            if used + cost > budget:
                full = False
                break
            if header_pending:
                lines.append(name)
                header_pending = False
            lines.append(line)
            used += cost
            shown += 1
        if not full:
            break

    rest = d.count - shown
    if rest:
        more = f"…и ещё {rest} {plural(rest, EVENTS)} на сайте"
        lines.append((f"\n{esc(more)}", f"\n{more}"))
    lines.append(footer)
    return "\n".join(line[0] for line in lines), shown


def visible_length(text: str) -> int:
    """Длина HTML-текста так, как её считает Telegram: без тегов, с раскрытыми сущностями."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))
