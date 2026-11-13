"""resolve_venues: текст места из анонса → площадка из справочника.

Одну площадку пишут по-разному: «ОЦКНТ», «ОЦКиНТ», «Центр культуры,
народного творчества и кино, ул. Космонавтов, 54А». Справочник
(sources/venues/<город>.yaml → meta.venues, meta.venue_aliases) хранит
каноничное название, адрес и варианты написания.

Как ищется площадка. Текст места и каждый вариант написания превращаются в
набор основ слов (как названия при склейке: без пунктуации, сокращений «ул.»,
«д.», инициалов, слова урезаны до 5 букв). Вариант подходит, если все его
основы есть в тексте. Очки площадки — основы самого длинного подошедшего
варианта плюс основы адреса, если адрес тоже весь есть в тексте. Побеждает
площадка с наибольшими очками; при равенстве — та, что найдена по названию,
а не только по адресу. Если и так ничья — место остаётся непривязанным.

Нечёткого сравнения нет намеренно: ложная привязка к чужой площадке хуже,
чем никакой. Непривязанные места видно в представлении
staging.unmatched_venues — это список того, что стоит добавить в справочник.

Площадка по умолчанию у источника (venue: в sources/<город>.yaml) — для
пабликов самих площадок: подставляется, если модель места не нашла или
написала что-то общее вроде «малый зал».

Модуль не знает про Airflow; run() работает с базой, остальное — чистые функции.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

STEM_LEN = 5  # как в dedupe.norm_title: «Есенинская» и «Есенина» → «есени»

_SYNONYMS = {"имени": "им"}

# Адресные сокращения и формы учреждений: в одном анонсе они есть, в другом нет.
_STOP_WORDS = (
    "ул", "улица", "улице", "пл", "площадь", "площади", "пр", "проспект", "стр", "строение",
    "корп", "корпус", "му", "мук", "маук", "мбук", "обук", "гбук", "гаук",
)  # fmt: skip

# Слова, которые сами по себе площадку не называют: «малый зал», «3 этаж».
_GENERIC_WORDS = (
    "большой", "большом", "малый", "малом", "зал", "зале", "этаж", "этаже", "сцена", "сцене",
    "фойе", "двор", "дворе", "внутренний", "главный", "атриум", "холл", "вход",
)  # fmt: skip


def _stem(word: str) -> str:
    return word if word[0].isdigit() else word[:STEM_LEN]


STOP = frozenset(_stem(w) for w in _STOP_WORDS)
GENERIC = frozenset(_stem(w) for w in _GENERIC_WORDS)


def venue_tokens(text: str | None) -> frozenset[str]:
    """Набор основ слов. «Культ-клуб «Атмосфера» (ул. Фрунзе, д.1)» → {культ, клуб, атмос, фрунз, 1}."""
    words = re.split(r"[^\w]+", (text or "").lower().replace("ё", "е"))
    tokens = set()
    for word in words:
        word = _SYNONYMS.get(word, word)
        if not word or word == "_" or (len(word) == 1 and not word.isdigit()):
            continue  # инициалы «М. М.», «д.», «г.»
        stem = _stem(word)
        if stem not in STOP:
            tokens.add(stem)
    return frozenset(tokens)


def is_generic(text: str | None) -> bool:
    """Место не названо: пусто, «большой зал», «3 этаж»."""
    return all(t in GENERIC or t[0].isdigit() for t in venue_tokens(text))


@dataclass(frozen=True)
class Venue:
    venue_id: int
    slug: str
    name: str
    address: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass
class VenueIndex:
    """Справочник площадок одного города в памяти."""

    venues: list[Venue]
    _names: list[tuple[Venue, list[frozenset]]] = field(init=False, repr=False)
    _addresses: dict[int, frozenset] = field(init=False, repr=False)

    def __post_init__(self):
        self._names = [(v, [venue_tokens(a) for a in (v.name, *v.aliases)]) for v in self.venues]
        self._addresses = {v.venue_id: venue_tokens(v.address) for v in self.venues if v.address}

    def scores(self, text: str | None) -> list[tuple[int, int, Venue]]:
        """(очки всего, очки по названию, площадка) для всех подошедших площадок, лучшие первыми."""
        tokens = venue_tokens(text)
        found = []
        for venue, variants in self._names:
            by_name = max((len(v) for v in variants if v and v <= tokens), default=0)
            address = self._addresses.get(venue.venue_id)
            by_address = len(address) if address and address <= tokens else 0
            if by_name or by_address:
                found.append((by_name + by_address, by_name, venue))
        return sorted(found, key=lambda f: (f[0], f[1]), reverse=True)

    def match(self, text: str | None) -> Venue | None:
        found = self.scores(text)
        if not found:
            return None
        if len(found) > 1 and found[0][:2] == found[1][:2]:
            log.debug("Место %r подходит к нескольким площадкам: %s", text, [f[2].slug for f in found])
            return None
        return found[0][2]


# matched — нашлась в справочнике, source_default — площадка паблика, unmatched — текст есть,
# но площадка не нашлась, no_venue — места нет вовсе, changed — у скольких строк привязка изменилась.
STAT_KEYS = ("matched", "source_default", "unmatched", "no_venue", "changed")


def load_indexes(cur) -> dict[int, VenueIndex]:
    """Справочники всех городов: city_id → VenueIndex."""
    cur.execute(
        """
        SELECT v.city_id, v.venue_id, v.slug, v.name, v.address,
               coalesce(array_agg(a.alias ORDER BY a.alias) FILTER (WHERE a.alias IS NOT NULL), '{}')
          FROM meta.venues v
          LEFT JOIN meta.venue_aliases a USING (venue_id)
         GROUP BY v.venue_id
         ORDER BY v.city_id, v.slug
        """
    )
    by_city: dict[int, list[Venue]] = {}
    for city_id, venue_id, slug, name, address, aliases in cur.fetchall():
        by_city.setdefault(city_id, []).append(Venue(venue_id, slug, name, address, tuple(aliases)))
    return {city_id: VenueIndex(venues) for city_id, venues in by_city.items()}


def run(conn, *, full: bool = False) -> dict:
    """Привязывает упоминания к площадкам.

    По умолчанию — только ещё не склеенные упоминания (их дальше возьмёт
    dedupe_events). full=True — все: после правки справочника вместе с
    пересборкой каталога.
    """
    stats = Counter()
    with conn, conn.cursor() as cur:
        indexes = load_indexes(cur)
        cur.execute("SELECT source_id, venue_id FROM meta.sources WHERE venue_id IS NOT NULL")
        defaults = dict(cur.fetchall())

        cur.execute(
            """
            SELECT id, city_id, source_id, venue, venue_id
              FROM staging.events
             WHERE validated AND quarantine_reason IS NULL
               AND (%s OR event_id IS NULL)
            """,
            (full,),
        )
        rows = cur.fetchall()
        for row_id, city_id, source_id, text, old_venue_id in rows:
            index = indexes.get(city_id)
            venue = index.match(text) if index else None
            venue_id = venue.venue_id if venue else None
            if venue_id is not None:
                stats["matched"] += 1
            elif source_id in defaults and is_generic(text):
                venue_id = defaults[source_id]
                stats["source_default"] += 1
            elif text:
                stats["unmatched"] += 1
            else:
                stats["no_venue"] += 1

            if venue_id != old_venue_id:
                cur.execute("UPDATE staging.events SET venue_id = %s WHERE id = %s", (venue_id, row_id))
                stats["changed"] += 1

    return {"checked": len(rows), **{key: stats[key] for key in STAT_KEYS}}
