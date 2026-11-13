"""Минимальный клиент VK API: только то, что нужно для загрузки стен.

Ошибки разделены на два вида:
- временные (сеть, 5xx, «слишком много запросов», дневной лимит) —
  пробрасываются как есть, Airflow повторит задачу;
- постоянные (неверный токен, сообщество закрыто или удалено) —
  повторять бессмысленно, DAG помечает задачу упавшей сразу.
"""

import logging
import os
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

API_URL = os.environ.get("VK_API_URL", "https://api.vk.com/method")
API_VERSION = "5.199"

# Коды ошибок: https://dev.vk.com/ru/reference/errors
RETRY_IN_PLACE = {6, 10}  # слишком много запросов в секунду, внутренняя ошибка VK
AUTH_ERRORS = {5, 27, 28}  # токен неверный или не того типа
# доступ закрыт, удалено, контент заблокирован, приватно, нет такого сообщества
ACCESS_ERRORS = {15, 18, 19, 30, 100, 203}


class VKError(Exception):
    def __init__(self, code: int | None, message: str, method: str):
        super().__init__(f"VK API {method}: [{code}] {message}")
        self.code = code
        self.method = method


class VKAuthError(VKError):
    """Проблема с токеном. Повтор не поможет."""


class VKAccessError(VKError):
    """Сообщество недоступно. Повтор не поможет."""


class VKClient:
    def __init__(
        self,
        token: str,
        *,
        api_url: str = API_URL,
        version: str = API_VERSION,
        min_interval: float = 0.35,
        max_retries: int = 3,
        timeout: float = 30,
        session: requests.Session | None = None,
    ):
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._version = version
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._timeout = timeout
        self._session = session or requests.Session()
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def call(self, method: str, **params: Any) -> Any:
        # POST, а не GET: токен не попадает в URL и, значит, в логи прокси.
        data = {**params, "access_token": self._token, "v": self._version}
        for attempt in range(self._max_retries + 1):
            self._throttle()
            resp = self._session.post(f"{self._api_url}/{method}", data=data, timeout=self._timeout)
            resp.raise_for_status()
            body = resp.json()
            error = body.get("error")
            if not error:
                return body["response"]

            code = error.get("error_code")
            message = error.get("error_msg", "")
            if code in RETRY_IN_PLACE and attempt < self._max_retries:
                delay = 2**attempt
                log.warning("VK %s: [%s] %s, повтор через %s с", method, code, message, delay)
                time.sleep(delay)
                continue
            if code in AUTH_ERRORS:
                raise VKAuthError(code, message, method)
            if code in ACCESS_ERRORS:
                raise VKAccessError(code, message, method)
            raise VKError(code, message, method)
        raise AssertionError("unreachable")

    def get_group(self, handle: str) -> dict:
        """Сообщество по короткому имени: id, name, screen_name, is_closed."""
        response = self.call("groups.getById", group_id=handle)
        # С версии 5.139 ответ — объект {"groups": [...]}, раньше был список.
        groups = response.get("groups", []) if isinstance(response, dict) else response
        if not groups:
            raise VKAccessError(None, f"сообщество {handle!r} не найдено", "groups.getById")
        return groups[0]

    def wall_get(self, owner_id: int, *, offset: int, count: int, wall_filter: str = "owner") -> list[dict]:
        response = self.call("wall.get", owner_id=owner_id, offset=offset, count=count, filter=wall_filter)
        return response.get("items", [])
