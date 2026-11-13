"""Справочник площадок: нормализация, поиск площадки по тексту места, загрузка и синхронизация yaml."""

from pathlib import Path

import pytest

from funfinder.events.venues import Venue, VenueIndex, is_generic, venue_tokens
from funfinder.sources import SourcesConfigError, load_city_config, load_venues, sync_city

REPO_SOURCES = Path(__file__).resolve().parents[1] / "sources"


@pytest.fixture(scope="module")
def lipetsk_index():
    venues = load_venues(REPO_SOURCES / "venues" / "lipetsk.yaml")
    return VenueIndex([Venue(i, v.slug, v.name, v.address, v.aliases) for i, v in enumerate(venues, 1)])


# --- Нормализация ---


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("Культ-клуб «Атмосфера» (ул. Фрунзе, д.1)", {"культ", "клуб", "атмос", "фрунз", "1"}),
        ("Библиотека им. М. М. Пришвина", {"библи", "им", "пришв"}),  # инициалы выброшены
        ("библиотека имени Есенина", {"библи", "им", "есени"}),
        ("ОЦКНТиК", {"оцкнт"}),
        ("Космонавтов, 15/3", {"космо", "15", "3"}),
        ("пл. Заводская, 9к2", {"завод", "9к2"}),
        ("Заводская площадь, 9к2", {"завод", "9к2"}),
        ("Звёздный", {"звезд"}),
        (None, set()),
    ],
)
def test_venue_tokens(text, tokens):
    assert venue_tokens(text) == tokens


@pytest.mark.parametrize(
    ("text", "generic"),
    [(None, True), ("", True), ("малый зал", True), ("Большой зал, 3й этаж", True), ("Прачечная", False)],
)
def test_is_generic(text, generic):
    assert is_generic(text) is generic


# --- Поиск площадки: реальные написания из анонсов Липецка ---


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("ОЦКиНТ", "ocknt"),
        ("г. Липецк, ОЦКНТ", "ocknt"),
        ("ул. Космонавтов, 54а (территория ОБУК ОЦКНТиК)", "ocknt"),
        ("Центр культуры, народного творчества и кино", "ocknt"),
        ("у областного центра культуры", "ocknt"),
        ("Прачечная, Заводская площадь, 9, 3 этаж", "prachechnaya"),
        ("Культ-клуб «Атмосфера» (ул. Фрунзе, д.1), малый зал", "atmosfera"),
        # «Динамо» в адресе Атмосферы не уводит событие во Дворец спорта
        ("Культ-клуб «Атмосфера», Липецк, Фрунзе 1, внутренний двор фабрики Динамо", "atmosfera"),
        ("Театр драмы им. Л.Н. Толстого", "teatr-dramy"),
        ("театр Толстого", "teatr-dramy"),
        ("драматический театр на Соколе", "teatr-na-sokole"),  # длиннее вариант — точнее
        ("Бартеньевская библиотека", "biblioteka-barteneva"),
        ("ул. Ленина, 31", "biblioteka-barteneva"),  # только адрес
        ("ЦГБ им. С.А. Есенина, ул. Космонавтов, 31", "biblioteka-esenina"),
        ("Библиотека «Сокольская», пл. Константиновой, 3", "biblioteka-sokolskaya"),
        ("ДК «Сокол» (пл. Константиновой, 3)", "dk-sokol"),
        # Филиал выигрывает у головного музея по адресу
        ("Липецкий историко-культурный музей, Городская управа, ул. Октябрьская, 57", "gorodskaya-uprava"),
        ("Липецкий историко-культурный музей, ул. Космонавтов, 2", "istoriko-kulturny-muzey"),
        ("Выставочный зал Липецкого областного краеведческого музея, ул. Ленина, 9А", "vystavochny-zal"),
        ("краеведческий музей, 2 этаж, Большой зал музея", "kraevedchesky-muzey"),
        ("филармония «Унион»", "filarmoniya"),
        ("СК «Звёздный»", "zvezdny"),
        ("Торгово-развлекательный центр «Ривьера», ул. Катукова, стр. 51", "riviera"),
    ],
)
def test_match_real_spellings(lipetsk_index, text, slug):
    venue = lipetsk_index.match(text)
    assert venue is not None and venue.slug == slug


@pytest.mark.parametrize(
    "text",
    [
        "пл. Константиновой, 3",  # по адресу — и ДК «Сокол», и библиотека: не угадываем
        "библиотека на улице Пришвина",  # не библиотека им. Пришвина
        "Мособлдрама",  # гастроли, а не площадка
        "малый зал",
        "Елец",
        None,
    ],
)
def test_no_match_when_unsure(lipetsk_index, text):
    assert lipetsk_index.match(text) is None


# --- Загрузка yaml ---


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


CITY = """
city: {slug: lipetsk, name: Липецк, timezone: Europe/Moscow}
vk:
  - handle: club1
    venue: atmosfera
  - handle: club2
"""

VENUES = """
- slug: atmosfera
  name: Культ-клуб «Атмосфера»
  address: ул. Фрунзе, 1
  aliases: [Атмосфера]
- slug: dacha
  name: Гастробар «Дача»
"""


def test_repo_venues_are_valid():
    cfg = load_city_config(REPO_SOURCES / "lipetsk.yaml")
    assert len(cfg.venues) > 30
    assert {s.venue for s in cfg.vk} >= {"atmosfera", "buksir", "prachechnaya"}


def test_city_without_venues_file(tmp_path):
    cfg = load_city_config(write(tmp_path / "lipetsk.yaml", CITY.replace("    venue: atmosfera\n", "")))
    assert cfg.venues == ()


@pytest.mark.parametrize(
    ("venues", "city"),
    [
        (VENUES + "- {slug: atmosfera, name: Другая}", CITY),  # slug дважды
        (VENUES + "- {slug: Bad_Slug, name: X}", CITY),
        (VENUES + "- {slug: other, name: Клуб, aliases: [Атмосфера]}", CITY),  # вариант у двух площадок
        (VENUES + "- {slug: other, name: Клуб, aliases: [малый зал]}", CITY),  # вариант ничего не называет
        (VENUES + "- {slug: other, name: Клуб, phone: 123}", CITY),  # опечатка в поле
        (VENUES, CITY.replace("venue: atmosfera", "venue: nowhere")),  # нет такой площадки
        ("slug: atmosfera", CITY),  # не список
    ],
)
def test_bad_venues_rejected(tmp_path, venues, city):
    write(tmp_path / "venues" / "lipetsk.yaml", venues)
    with pytest.raises(SourcesConfigError):
        load_city_config(write(tmp_path / "lipetsk.yaml", city))


# --- Синхронизация с базой ---


def venues_in_db(conn):
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.slug, v.address, array_agg(a.alias ORDER BY a.alias) FILTER (WHERE a.alias IS NOT NULL)
              FROM meta.venues v LEFT JOIN meta.venue_aliases a USING (venue_id)
             GROUP BY v.venue_id ORDER BY v.slug
            """
        )
        venues = cur.fetchall()
        cur.execute(
            "SELECT s.handle, v.slug FROM meta.sources s LEFT JOIN meta.venues v USING (venue_id) ORDER BY s.handle"
        )
        return venues, cur.fetchall()


def test_sync_venues_and_source_defaults(conn, tmp_path):
    write(tmp_path / "venues" / "lipetsk.yaml", VENUES)
    cfg = load_city_config(write(tmp_path / "lipetsk.yaml", CITY))
    assert sync_city(conn, cfg)["venues"] == 2
    sync_city(conn, cfg)  # повтор ничего не ломает
    assert venues_in_db(conn) == (
        [("atmosfera", "ул. Фрунзе, 1", ["Атмосфера"]), ("dacha", None, None)],
        [("club1", "atmosfera"), ("club2", None)],
    )

    # Площадку убрали из справочника, у другой поменялись варианты написания.
    write(tmp_path / "venues" / "lipetsk.yaml", "- {slug: dacha, name: Дача, aliases: [Гастробар Дача]}")
    sync_city(conn, load_city_config(write(tmp_path / "lipetsk.yaml", CITY.replace("    venue: atmosfera\n", ""))))
    assert venues_in_db(conn) == ([("dacha", None, ["Гастробар Дача"])], [("club1", None), ("club2", None)])
