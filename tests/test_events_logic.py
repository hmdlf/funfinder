"""Логика process_events без базы и без LLM: очистка полей, разбор ответа, сравнение названий."""

from datetime import UTC, date, datetime, time
from types import SimpleNamespace

import httpx
import openai
import pytest

from funfinder.events.dedupe import norm_title, times_compatible, title_score
from funfinder.events.extract import clean_date, clean_status, clean_text, clean_time, post_text
from funfinder.events.llm import LLMClient, LLMConfigError, build_prompt, build_user_message, parse_answer


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-13", date(2026, 11, 13)),
        ("2026-11-25Tnull:null", date(2026, 11, 25)),  # так модель писала, когда не знала время
        ("15.11", None),
        (None, None),
        (20261113, None),
    ],
)
def test_clean_date(value, expected):
    assert clean_date(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("19:00", time(19, 0)),
        ("9:30", time(9, 30)),
        ("19:00-20:15", time(19, 0)),
        ("25:00", None),
        ("вечером", None),
        (None, None),
    ],
)
def test_clean_time(value, expected):
    assert clean_time(value) == expected


def test_clean_text_and_status():
    assert clean_text("  null ") is None
    assert clean_text("Дача") == "Дача"
    assert clean_text("x" * 600) == "x" * 500
    assert clean_status("Cancelled") == "cancelled"
    assert clean_status("отменено") == "scheduled"


def test_post_text_includes_repost():
    assert post_text("  ", "") == ""
    assert post_text("Смотрите!", "Концерт 20.11") == "Смотрите!\n\n[Репост]\nКонцерт 20.11"


def test_parse_answer_strips_code_fence():
    assert parse_answer('```json\n{"events": [{"title": "A"}]}\n```') == [{"title": "A"}]
    assert parse_answer('{"events": []}') == []


@pytest.mark.parametrize("answer", ['{"events": [{}]}}', '{"items": []}', '{"events": "нет"}', "Не могу"])
def test_parse_answer_rejects_bad_json(answer):
    with pytest.raises((ValueError, KeyError, TypeError)):
        parse_answer(answer)


def test_prompt_and_user_message():
    prompt = build_prompt([("concert", "Концерты"), ("quiz", "Квизы")])
    assert "- quiz (Квизы)" in prompt
    assert "<categories>" not in prompt

    published = datetime(2026, 11, 7, 22, 30, tzinfo=UTC)  # в Москве уже 8-е, воскресенье
    message = build_user_message("Текст", city="Липецк", published_at=published, timezone="Europe/Moscow")
    assert message.startswith("Город: Липецк\nДата публикации: 2026-11-08 01:30 (воскресенье)")


def test_norm_title_drops_noise_and_endings():
    assert norm_title("Игра «Угадай мелодию. 160 русских хитов» (Новинка!)") == "угада мелод 160 русск хитов"
    assert norm_title("Кинопоказ: «Толкин»") == norm_title("Кинопоказ «Толкин»")
    assert norm_title("Выступление Султана Лагучева") == norm_title("Султан Лагучев")


# Пары из настоящей витрины Липецка. Внутри пары — одна дата и близкое время,
# так что решает только похожесть названий.
@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Вау Квиз «Кино и музыка СССР»", "Угадай. Кино и музыка СССР", True),
        ("ВАУ! На все сто (Новинка!)", "ВАУ! На все сто", True),
        ("НЕВОЗМОЖНОЕ ВЕЧНОЕ", "Спектакль «Невозможное, вечное...»", True),
        ("Вечер страшных историй", "Вечер страшных историй: рассказ «Семья вурдалака»", True),
        ("Выступление Султана Лагучева", "Султан Лагучев", True),
        # «ВАУ КВИЗ» после нормализации — одно слово «вау», оно входит во всё подряд.
        ("ВАУ КВИЗ", "ВАУ! На все сто", False),
        ("Танго / Группа С / Сергей Сохненко", "Танго Барре «Новый набор» с Анной Сохненко", False),
        ("Танго Барре «Продолжающие» с Анной Сохненко", "Мужские техники с Сергеем Сохненко", False),
        ("GYROKINESIS® c Анна Сохненко", "Танго / Группа B / Сергей Сохненко", False),
        # «Танго / Группа B» и «Танго / Группа D» по названию почти одинаковы (одна буква) —
        # их разводит время: у групп разные часы занятий.
        ("Дело о радости: встреча с актрисой Диной Кюнбергер", "Спектакль «Бывшие» с Андреем Соколовым", False),
    ],
)
def test_title_score(a, b, same):
    assert (title_score(norm_title(a), norm_title(b)) >= 80) is same


def test_times_compatible():
    assert times_compatible(None, time(19, 0))
    assert times_compatible(time(19, 0), time(19, 1))  # Прачечная пишет 19:01, перепост — 19:00
    assert times_compatible(time(19, 0), time(19, 30))
    assert not times_compatible(time(19, 0), time(19, 31))
    assert not times_compatible(time(11, 30), time(14, 0))  # разные сеансы студии


# --- LLMClient с поддельным API: таймауты, запасная модель, фильтр, ошибки настроек ---


def _response(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def _client(*outcomes):
    """LLMClient, у которого модели по очереди отвечают outcomes: ответ или исключение."""
    llm = LLMClient("key", "folder", models=("first", "second"))
    calls = iter(outcomes)

    def create(**kwargs):
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return llm


_REQUEST = httpx.Request("POST", "https://ai.api.cloud.yandex.net/v1/chat/completions")


def test_llm_timeout_falls_back_to_next_model():
    llm = _client(openai.APITimeoutError(request=_REQUEST), _response('{"events": [{"title": "A"}]}'))
    result = llm.extract("prompt", "post")
    assert (result.status, result.model, result.events) == ("ok", "second", [{"title": "A"}])


def test_llm_all_models_timed_out():
    llm = _client(openai.APITimeoutError(request=_REQUEST), openai.APITimeoutError(request=_REQUEST))
    assert llm.extract("prompt", "post").status == "timeout"


def test_llm_content_filter_then_refused():
    llm = _client(_response("Не могу", "content_filter"), _response("Не могу", "content_filter"))
    assert llm.extract("prompt", "post").status == "refused"


def test_llm_access_error_is_config_error():
    forbidden = openai.PermissionDeniedError(
        "Forbidden", response=httpx.Response(403, request=_REQUEST, text="Forbidden"), body=None
    )
    with pytest.raises(LLMConfigError):
        _client(forbidden).extract("prompt", "post")
