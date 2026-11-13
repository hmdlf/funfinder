"""Вызов LLM через Yandex AI Studio (OpenAI-совместимый API) и разбор ответа.

Модуль не знает ни про базу, ни про Airflow: на вход системный промпт и
сообщение с постом, на выход — список событий и служебная информация.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import openai
from openai import OpenAI

log = logging.getLogger(__name__)

PROMPT_VERSION = "v5"  # меняем при каждой правке промпта — версия пишется в журнал
MODELS = ("aliceai-llm-flash", "qwen3.6-35b-a3b/latest")  # вторая — запасная, если первая отказала
BASE_URL = "https://ai.api.cloud.yandex.net/v1"
# Дайджест «афиша на месяц» — это десятки событий, модель пишет такой ответ долго.
REQUEST_TIMEOUT = openai.Timeout(120.0, connect=10.0)

STATUSES = ("scheduled", "cancelled", "postponed")
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

_PROMPT_TEMPLATE = """Ты извлекаешь анонсы событий из постов городских пабликов.
Найди в посте все события, которые можно посетить: концерты, спектакли, выставки, лекции, квизы, \
вечеринки, мастер-классы, сходки и т.п.

Даты и время:
- Явная дата в тексте («8.11», «15 ноября») важнее относительных слов («завтра», «в субботу»).
- Относительные даты считай от даты публикации поста.
- Если год не указан, бери ближайшую такую дату после даты публикации.
- Если время не указано, ставь time: null. Не подставляй время публикации поста.
- Если дату события определить нельзя, ставь date: null.

Не извлекай:
- события, которые уже прошли на момент публикации поста (фотоотчёты, итоги, благодарности участникам);
- розыгрыши, акции и рекламу без конкретного события;
- новости и рассказы о жизни места, куда никого не зовут: «кенгуру гуляют в вольере», «в парке зацвели тюльпаны», режим работы. Выставку, экскурсию или праздник с датой — извлекай;
- события в других городах. Город указан в сообщении; события в его области — можно.

Если пост сообщает об отмене или переносе события, всё равно верни это событие с нужным status.

Ответь ТОЛЬКО JSON, без пояснений и без ```:
{"events": [{"title": "...", "date": "ГГГГ-ММ-ДД", "time": "ЧЧ:ММ", "venue": "...", "price": "...", \
"category": "...", "status": "..."}]}
category — один код из списка (в скобках — что он значит):
<categories>
status — одно из: <statuses>.
venue и price — только если они написаны в посте, иначе null.
venue — название места или адрес. Хэштег (#прачечная_учреждениекультуры) — не место.
Если событий нет — {"events": []}."""


def build_prompt(categories: list[tuple[str, str]]) -> str:
    """Системный промпт. categories — пары (slug, название) из meta.categories."""
    listing = "\n".join(f"- {slug} ({name})" for slug, name in categories)
    return _PROMPT_TEMPLATE.replace("<categories>", listing).replace("<statuses>", ", ".join(STATUSES))


def format_published(dt: datetime, timezone: str) -> str:
    """Дата публикации для модели: местное время города и день недели.
    День недели подсказываем явно: считать по календарю модели умеют плохо."""
    local = dt.astimezone(ZoneInfo(timezone))
    return f"{local:%Y-%m-%d %H:%M} ({WEEKDAYS[local.weekday()]})"


def build_user_message(text: str, *, city: str, published_at: datetime, timezone: str) -> str:
    return f"Город: {city}\nДата публикации: {format_published(published_at, timezone)}\n\n{text}"


def parse_answer(answer: str) -> list[dict]:
    """Убирает обёртку ```json ... ``` и достаёт список событий.
    Бросает ValueError, KeyError или TypeError, если ответ не тот."""
    clean = re.sub(r"^\s*```(?:json)?|```\s*$", "", answer.strip()).strip()
    events = json.loads(clean)["events"]
    if not isinstance(events, list):
        raise TypeError("events — не список")
    return [e for e in events if isinstance(e, dict)]


class LLMConfigError(Exception):
    """Ошибка 4xx (ключ, права, имя модели): повтор не поможет, нужна правка настроек."""


@dataclass
class LLMResult:
    status: str  # ok / refused / invalid_json / timeout / skipped_empty
    model: str | None = None
    events: list[dict] = field(default_factory=list)
    raw_answer: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0


class LLMClient:
    def __init__(self, api_key: str, folder_id: str, models: tuple[str, ...] = MODELS):
        self.folder_id = folder_id
        self.models = models
        # max_retries: SDK сам повторяет 429, 5xx и таймауты с паузой. Один повтор:
        # дальше либо следующая модель, либо ретрай всей задачи в Airflow.
        self.client = OpenAI(
            api_key=api_key, base_url=BASE_URL, project=folder_id, timeout=REQUEST_TIMEOUT, max_retries=1
        )

    def extract(self, system_prompt: str, user_message: str) -> LLMResult:
        """Пробует модели по очереди, пока одна не ответит без отказа фильтра и без таймаута.

        Таймаут — не повод валить весь прогон: один «тяжёлый» пост иначе блокировал бы
        все остальные. Пост получает статус timeout и перепрогоняется в следующем запуске.
        """
        timed_out = False
        for model in self.models:
            try:
                resp = self.client.chat.completions.create(
                    model=f"gpt://{self.folder_id}/{model}",
                    temperature=0,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                )
            except openai.APITimeoutError:
                log.warning("%s: нет ответа за %.0f с, пробую следующую модель", model, REQUEST_TIMEOUT.read)
                timed_out = True
                continue
            except openai.APIStatusError as exc:
                if 400 <= exc.status_code < 500 and exc.status_code != 429:
                    raise LLMConfigError(f"{model}: {exc.status_code} {exc.response.text}") from exc
                raise  # 429 и 5xx после повторов SDK — пусть Airflow повторит задачу

            choice = resp.choices[0]
            if choice.finish_reason == "content_filter":
                continue  # тематический фильтр модели — пробуем следующую

            usage = resp.usage
            answer = choice.message.content or ""
            result = LLMResult(
                status="ok",
                model=model,
                raw_answer=answer,
                tokens_in=(usage.prompt_tokens or 0) if usage else 0,
                tokens_out=(usage.completion_tokens or 0) if usage else 0,
            )
            try:
                result.events = parse_answer(answer)
            except (ValueError, KeyError, TypeError):
                result.status = "invalid_json"
            return result

        # Ни одна модель не ответила. Если хоть одна не успела — попробуем в следующий раз.
        return LLMResult(status="timeout" if timed_out else "refused")
