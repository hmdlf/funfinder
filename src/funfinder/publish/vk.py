"""Подборка для сообщества VK: полуавтомат (Draft) или запись на стене через API (Wall).

Полуавтомат. Право «Стена» VK новым приложениям VK ID больше не выдаёт (2026),
поэтому основной путь — Draft: бот присылает готовый текст для VK человеку в
Telegram (в блоке, который копируется нажатием), а публикует он сам. Ссылки в
тексте — в разметке VK [https://vk.com/wall-…|Название]: при публикации VK
превратит их в ссылки-слова.

Через API — Wall, ниже. Работает, если у приложения VK ID есть право «Стена».

Ключ — пользовательский, из VK ID (funfinder.publish.vkid): wall.post с ключом
сообщества не работает. Человек, который входил, должен быть редактором или
администратором сообщества — иначе от имени сообщества не опубликовать.

Перед публикацией сообщество ищется по адресу из yaml (groups.getById): так
ошибка «не админ» или «нет такого сообщества» видна сразу и понятным текстом.

Дубли. У wall.post есть параметр guid: запись с тем же guid в течение часа
второй раз не создаётся. guid строится из места, дня подборки и запуска DAG,
поэтому повтор задачи после сетевого сбоя не опубликует подборку дважды, а
ручной запуск с force (новый запуск — новый guid) опубликует.

Ошибки VK (https://dev.vk.com/ru/reference/errors):
- сеть, 1, 6, 9, 10 — сбой или «слишком часто»: повтор поможет;
- 5 — ключ не принят: один раз обновляем его и пробуем снова; не помогло —
  нужен новый вход;
- 14, 15, 17, 214, 222 и прочие — нужен человек: капча, нет прав, стена
  закрыта, ссылки запрещены.
"""

import hashlib
import html
import logging

import requests

from funfinder.publish.errors import SendError
from funfinder.publish.telegram import Bot
from funfinder.publish.vkid import LOGIN_COMMAND
from funfinder.vk.client import VKAuthError, VKClient, VKError

log = logging.getLogger(__name__)

RETRYABLE = {1, 6, 9, 10}
HINTS = {
    14: "VK требует капчу: опубликуйте одну запись в сообществе вручную и запустите publish снова",
    15: "нет доступа: у ключа нет права «Стена» (см. «Доступы» приложения VK ID) или вы не редактор сообщества",
    17: "VK требует подтвердить вход: откройте vk.com в браузере под этим аккаунтом",
    100: "VK не принял параметры записи",
    214: "публиковать нельзя: стена сообщества закрыта или исчерпан лимит записей на сегодня",
    222: "VK запретил ссылки в этой записи",
}


class VKPublishError(SendError):
    pass


def group_url(handle: str) -> str:
    """Адрес сообщества из handle в базе: '123' → https://vk.com/club123, 'matingu' → https://vk.com/matingu."""
    return f"https://vk.com/club{handle}" if handle.isdigit() else f"https://vk.com/{handle}"


class Draft:
    """Полуавтомат: send(группа, текст VK) отправляет текст в чат Telegram, возвращает 'draft:<id сообщения>'.

    Текст — в блоке <pre>: в Telegram он копируется нажатием целиком, ссылки в нём
    не превращаются в превью и не портят разметку VK.
    """

    def __init__(self, bot: Bot, chat: str):
        self.bot = bot
        self.chat = chat

    @staticmethod
    def message(handle: str, text: str) -> str:
        return (
            f"<b>Подборка для VK</b> — опубликуйте в сообществе {html.escape(group_url(handle))}\n"
            "Нажмите на блок с текстом, чтобы скопировать.\n"
            f"<pre>{html.escape(text)}</pre>"
        )

    def send(self, handle: str, text: str, *, key: str | None = None, link: str | None = None) -> str:
        return f"draft:{self.bot.send(self.chat, self.message(handle, text))}"

    def close(self) -> None:
        self.bot.close()


def guid(key: str) -> str:
    """Ключ выпуска → guid для wall.post: короткий и без лишних символов."""
    return hashlib.sha256(key.encode()).hexdigest()[:32]


class Wall:
    """Отправитель для run.publish: send(группа, текст) → 'wall-<id>_<post>'.

    tokens — объект с методом access_token(force_refresh=False), обычно vkid.TokenStore.
    """

    def __init__(self, tokens, *, client_factory=VKClient):
        self.tokens = tokens
        self.client_factory = client_factory

    def _call(self, method: str, **params):
        """Вызов с живым ключом. Ключ не принят — обновляем один раз и повторяем."""
        for attempt in (1, 2):
            client = self.client_factory(self.tokens.access_token(force_refresh=attempt == 2))
            try:
                return client.call(method, **params)
            except VKAuthError as exc:
                if attempt == 1:
                    log.warning("VK не принял ключ (%s), обновляю и пробую ещё раз", exc)
                    continue
                raise VKPublishError(
                    f"{exc}. Ключ не принят и после обновления. Войдите заново: {LOGIN_COMMAND}", retryable=False
                ) from None
            except VKError as exc:
                hint = HINTS.get(exc.code)
                raise VKPublishError(
                    f"{exc}" + (f" — {hint}" if hint else ""), retryable=exc.code in RETRYABLE
                ) from None
            except requests.RequestException as exc:
                raise VKPublishError(f"VK {method}: сеть: {type(exc).__name__}", retryable=True) from None
        raise AssertionError("unreachable")

    def group(self, handle: str) -> dict:
        """Сообщество по адресу из yaml + проверка, что вошедший может публиковать от его имени."""
        response = self._call("groups.getById", group_id=handle, fields="is_admin,admin_level")
        groups = response.get("groups", []) if isinstance(response, dict) else response
        if not groups:
            raise VKPublishError(f"VK: сообщество {handle!r} не найдено", retryable=False)
        group = groups[0]
        # admin_level: 1 — модератор, 2 — редактор, 3 — администратор. Публиковать от имени
        # сообщества может редактор и выше. Если VK не прислал is_admin, не гадаем: решит wall.post.
        is_admin, level = group.get("is_admin"), group.get("admin_level")
        if is_admin == 0 or (is_admin and level is not None and level < 2):
            raise VKPublishError(
                f"VK: вошедший пользователь не редактор и не администратор сообщества «{group.get('name', handle)}» "
                "— от его имени не опубликовать",
                retryable=False,
            )
        return group

    def send(self, handle: str, text: str, *, key: str | None = None, link: str | None = None) -> str:
        """Запись на стене сообщества. link — ссылка-вложение (карточка афиши под текстом)."""
        group_id = int(self.group(handle)["id"])
        params = {"owner_id": -group_id, "from_group": 1, "message": text}
        if link:
            params["attachments"] = link
        if key:
            params["guid"] = guid(key)
        response = self._call("wall.post", **params)
        return f"wall-{group_id}_{response['post_id']}"

    def close(self) -> None:
        pass
