"""process_events на настоящей базе: извлечение, проверка, склейка, повторы и правки.

Вместо LLM — FakeLLM: отвечает заранее заданными событиями по ключевой фразе в посте.
"""

import json

from conftest import NOW, FakeVK, make_post
from funfinder.events import dedupe, extract, validate
from funfinder.events.llm import LLMResult
from funfinder.vk.ingest import IngestSettings, ingest_source

SETTINGS = IngestSettings(page_size=50, initial_lookback_days=30, recheck_days=3)

QUIZ = "Угадай мелодию. 100 русских суперхитов"
DACHA = "Гастробар «Дача», ул. Интернациональная, 6"


class FakeLLM:
    def __init__(self, answers: dict):
        self.answers = answers  # фраза в посте → события, "REFUSED", "INVALID" или "TIMEOUT"
        self.messages: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.messages)

    def extract(self, system_prompt: str, user_message: str) -> LLMResult:
        self.messages.append(user_message)
        for key, answer in self.answers.items():
            if key in user_message:
                if answer == "REFUSED":
                    return LLMResult(status="refused")
                if answer == "TIMEOUT":
                    return LLMResult(status="timeout")
                if answer == "INVALID":
                    return LLMResult(status="invalid_json", model="fake", raw_answer='{"events": [}')
                raw = json.dumps({"events": answer}, ensure_ascii=False)
                return LLMResult(status="ok", model="fake", events=answer, raw_answer=raw, tokens_in=100)
        return LLMResult(status="ok", model="fake", raw_answer='{"events": []}')


def ev(title, day, at=None, venue=None, category="quiz", status="scheduled"):
    """Событие так, как его вернула бы модель."""
    return {
        "title": title,
        "date": day,
        "time": at,
        "venue": venue,
        "price": None,
        "category": category,
        "status": status,
    }


def load(conn, source, posts, run_id="run_1"):
    ingest_source(FakeVK(posts), conn, source, run_id=run_id, settings=SETTINGS, now=NOW)


def pipeline(conn, llm):
    return extract.run(conn, llm, now=NOW), validate.run(conn), dedupe.run(conn)


def catalog(conn):
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.title, e.event_date::text, e.event_time::text, e.venue, e.category, e.status,
                   (SELECT count(*) FROM staging.events s WHERE s.event_id = e.event_id)
              FROM catalog.events e
             ORDER BY e.event_date, e.event_time NULLS LAST, e.title
            """
        )
        return cur.fetchall()


def test_duplicates_are_merged_and_fields_filled(conn, lipetsk):
    load(
        conn,
        lipetsk,
        [
            make_post(1, days_ago=3, text="Анонс квиза"),
            make_post(2, days_ago=2, text="Напоминание про квиз"),
            make_post(3, days_ago=1, text="Новая игра"),
        ],
    )
    llm = FakeLLM(
        {
            "Анонс квиза": [ev(f"{QUIZ} (с туром «Двое из ларца»)", "2026-11-13", "19:00")],
            "Напоминание": [ev(f"Игра «{QUIZ}»", "2026-11-13", venue=DACHA)],  # без времени, зато с адресом
            "Новая игра": [ev("ВАУ! На все сто", "2026-11-11", "19:00", "Гастробар «Дача»")],
        }
    )
    _, _, stats = pipeline(conn, llm)

    assert stats == {"mentions": 3, "created": 2, "linked": 1, "deleted": 0}
    assert catalog(conn) == [
        ("ВАУ! На все сто", "2026-11-11", "19:00:00", "Гастробар «Дача»", "quiz", "scheduled", 1),
        (f"{QUIZ} (с туром «Двое из ларца»)", "2026-11-13", "19:00:00", DACHA, "quiz", "scheduled", 2),
    ]


def test_rerun_does_not_call_llm_or_change_catalog(conn, lipetsk):
    load(conn, lipetsk, [make_post(1, days_ago=1, text="Анонс квиза")])
    llm = FakeLLM({"Анонс квиза": [ev(QUIZ, "2026-11-13", "19:00", DACHA)]})
    pipeline(conn, llm)
    before = catalog(conn)

    extract_stats, _, dedupe_stats = pipeline(conn, llm)
    assert llm.calls == 1
    assert extract_stats == {"posts": 0}
    assert dedupe_stats == {"mentions": 0, "created": 0, "linked": 0, "deleted": 0}
    assert catalog(conn) == before


def test_reminder_in_later_run_links_to_existing_event(conn, lipetsk):
    first = make_post(1, days_ago=3, text="Анонс квиза")
    llm = FakeLLM(
        {
            "Анонс квиза": [ev(QUIZ, "2026-11-13", "19:00", DACHA)],
            "Уже завтра": [ev(f"Игра «{QUIZ}»", "2026-11-13", "19:00")],
        }
    )
    load(conn, lipetsk, [first])
    pipeline(conn, llm)

    load(conn, lipetsk, [first, make_post(2, days_ago=0.5, text="Уже завтра играем!")], run_id="run_2")
    extract_stats, _, dedupe_stats = pipeline(conn, llm)

    assert extract_stats["posts"] == 1  # только новый пост
    assert dedupe_stats == {"mentions": 1, "created": 0, "linked": 1, "deleted": 0}
    assert len(catalog(conn)) == 1
    assert catalog(conn)[0][-1] == 2


def test_different_sessions_stay_separate(conn, lipetsk):
    load(conn, lipetsk, [make_post(1, days_ago=1, text="Расписание студии")])
    title = "Студия «Рисую тут»"
    llm = FakeLLM(
        {
            "Расписание": [
                ev(title, "2026-11-15", "11:30", category="workshop"),
                ev(title, "2026-11-15", "14:00", category="workshop"),
                ev(title, "2026-11-15", None, category="workshop"),  # без времени прилипнет к одному из сеансов
            ]
        }
    )
    pipeline(conn, llm)
    assert [(r[2], r[-1]) for r in catalog(conn)] == [("11:30:00", 2), ("14:00:00", 1)]


def test_quarantine_rules(conn, lipetsk):
    # Пост опубликован 7 ноября (по Москве).
    load(conn, lipetsk, [make_post(1, days_ago=1, text="Всякое")])
    llm = FakeLLM(
        {
            "Всякое": [
                ev("Без даты", None),
                ev("Фотоотчёт", "2026-11-01", "19:00"),
                ev("Ошибка с годом", "2028-11-20", "19:00"),
                ev(None, "2026-11-20"),
                ev("Детский праздник", "2026-11-20", "12:00", "ДК", category="детское"),
            ]
        }
    )
    _, stats, _ = pipeline(conn, llm)

    assert stats == {
        "no_title": 1,
        "no_date": 1,
        "past_event": 1,
        "date_too_far": 1,
        "category_fixed": 1,  # «детское» → other
        "checked": 5,
        "passed": 1,
    }
    assert catalog(conn) == [("Детский праздник", "2026-11-20", "12:00:00", "ДК", "other", "scheduled", 1)]
    with conn, conn.cursor() as cur:
        cur.execute("SELECT level, reason FROM quarantine.events ORDER BY reason")
        assert cur.fetchall() == [
            ("event", "date_too_far"),
            ("event", "no_date"),
            ("event", "no_title"),
            ("event", "past_event"),
        ]


def test_edited_post_updates_status(conn, lipetsk):
    post = make_post(1, days_ago=1, text="Фестиваль в субботу")
    llm = FakeLLM(
        {
            "Фестиваль в субботу": [ev("Фестиваль «Доброе сало»", "2026-11-14", "12:00", "Добринка", "festival")],
            "ОТМЕНЁН": [ev("Фестиваль «Доброе сало»", "2026-11-14", "12:00", status="cancelled", category="festival")],
        }
    )
    load(conn, lipetsk, [post])
    pipeline(conn, llm)

    post["text"] = "Фестиваль ОТМЕНЁН по решению организаторов"
    load(conn, lipetsk, [post], run_id="run_2")
    extract_stats, _, dedupe_stats = pipeline(conn, llm)

    assert extract_stats["posts"] == 1  # правку заметили по content_hash
    assert dedupe_stats["linked"] == 1
    # Старое упоминание заменено новым: поля события — из актуальной версии поста.
    assert catalog(conn) == [("Фестиваль «Доброе сало»", "2026-11-14", "12:00:00", None, "festival", "cancelled", 1)]


def test_event_removed_from_edited_post_leaves_catalog(conn, lipetsk):
    post = make_post(1, days_ago=1, text="Выставка открывается")
    llm = FakeLLM({"Выставка открывается": [ev("Выставка «Я люблю тебя, Россия»", "2026-11-11", "14:00", "Музей")]})
    load(conn, lipetsk, [post])
    pipeline(conn, llm)
    assert len(catalog(conn)) == 1

    post["text"] = "Пост больше не про выставку"
    load(conn, lipetsk, [post], run_id="run_2")
    _, _, dedupe_stats = pipeline(conn, llm)

    assert dedupe_stats["deleted"] == 1
    assert catalog(conn) == []


def test_empty_refused_and_invalid_posts_are_journaled(conn, lipetsk):
    load(
        conn,
        lipetsk,
        [
            make_post(1, days_ago=1, text=" "),  # только картинка
            make_post(2, days_ago=1, text=" ", copy_history=[{"text": "Концерт 20.11 в 19:00"}]),
            make_post(3, days_ago=1, text="Пост про спецоперацию"),
            make_post(4, days_ago=1, text="Сломанный ответ"),
        ],
    )
    llm = FakeLLM(
        {
            "[Репост]\nКонцерт": [ev("Концерт", "2026-11-20", "19:00", category="concert")],
            "спецоперацию": "REFUSED",
            "Сломанный": "INVALID",
        }
    )
    stats, _, _ = pipeline(conn, llm)

    assert llm.calls == 3  # пустой пост в модель не отправляли
    assert stats["skipped_empty"] == 1 and stats["refused"] == 1 and stats["invalid_json"] == 1 and stats["ok"] == 1
    with conn, conn.cursor() as cur:
        cur.execute("SELECT level, reason FROM quarantine.events ORDER BY reason")
        assert cur.fetchall() == [("post", "invalid_json"), ("post", "refused")]
        cur.execute("SELECT count(*) FROM catalog.events_public")  # витрина собирается без ошибок
        assert cur.fetchone()[0] >= 0


def test_llm_gets_city_weekday_and_repost(conn, lipetsk):
    load(conn, lipetsk, [make_post(1, days_ago=1, text="Смотрите!", copy_history=[{"text": "Лекция в пятницу"}])])
    llm = FakeLLM({})
    pipeline(conn, llm)
    # NOW — 8 ноября 12:00 UTC, пост на сутки раньше: 7 ноября 15:00 по Москве, суббота.
    assert llm.messages == [
        "Город: Липецк\nДата публикации: 2026-11-07 15:00 (суббота)\n\nСмотрите!\n\n[Репост]\nЛекция в пятницу"
    ]


def test_rebuild_catalog_without_llm(conn, lipetsk):
    load(
        conn,
        lipetsk,
        [make_post(1, days_ago=2, text="Анонс спектакля"), make_post(2, days_ago=1, text="Репост спектакля")],
    )
    llm = FakeLLM(
        {
            "Анонс": [ev("Спектакль «Невозможное, вечное...»", "2026-11-12", "19:01", "Прачечная", "theatre")],
            "Репост": [ev("НЕВОЗМОЖНОЕ ВЕЧНОЕ", "2026-11-12", "19:00", category="theatre")],
        }
    )
    pipeline(conn, llm)
    # Имитируем каталог, склеенный старыми правилами: те же упоминания, но два события.
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE staging.events SET event_id = NULL")
        cur.execute("DELETE FROM catalog.events")
        cur.execute(
            """
            INSERT INTO catalog.events (city_id, title, title_norm, event_date, event_time, category)
            SELECT city_id, title, 'старая норма ' || id, event_date, event_time, category FROM staging.events
            """
        )
        cur.execute(
            """
            UPDATE staging.events s SET event_id = e.event_id
              FROM catalog.events e WHERE e.title = s.title
            """
        )
    assert len(catalog(conn)) == 2

    calls = llm.calls
    stats = dedupe.run(conn, rebuild=True)

    assert llm.calls == calls  # модель не вызывалась
    assert stats == {"mentions": 2, "created": 1, "linked": 1, "deleted": 0}
    assert catalog(conn) == [
        ("Спектакль «Невозможное, вечное...»", "2026-11-12", "19:01:00", "Прачечная", "theatre", "scheduled", 2)
    ]


def test_reextract_reprocesses_only_old_prompt_versions(conn, lipetsk):
    load(conn, lipetsk, [make_post(1, days_ago=2, text="Первый"), make_post(2, days_ago=1, text="Второй")])
    llm = FakeLLM({})
    extract.run(conn, llm, now=NOW)
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE staging.vk_post_extractions SET prompt_version = 'v3' WHERE post_id = 1")

    assert extract.run(conn, llm, now=NOW) == {"posts": 0}  # без флага старые версии не трогаем
    assert extract.run(conn, llm, reextract=True, now=NOW)["posts"] == 1  # только пост со старым промптом
    assert extract.run(conn, llm, reextract=True, now=NOW) == {"posts": 0}  # повтор ничего не делает


def test_timeout_post_does_not_block_others_and_is_retried(conn, lipetsk):
    load(
        conn,
        lipetsk,
        [make_post(1, days_ago=2, text="Огромный дайджест"), make_post(2, days_ago=1, text="Анонс квиза")],
    )
    llm = FakeLLM({"Огромный дайджест": "TIMEOUT", "Анонс квиза": [ev(QUIZ, "2026-11-13", "19:00", DACHA)]})

    stats, _, _ = pipeline(conn, llm)
    assert stats["timeout"] == 1 and stats["ok"] == 1  # второй пост обработан, хотя первый не успел
    assert len(catalog(conn)) == 1

    # В следующем запуске модель ответила — пост перепрогнан, остальные не тронуты.
    llm.answers["Огромный дайджест"] = [ev("Земля Эльзы", "2026-11-12", "19:00", "Театр драмы", "theatre")]
    stats, _, _ = pipeline(conn, llm)
    assert stats == {"ok": 1, "events": 1, "tokens_in": 100, "tokens_out": 0, "posts": 1}
    assert len(catalog(conn)) == 2
