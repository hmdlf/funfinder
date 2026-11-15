"""Общая ошибка отправки: от неё наследуются ошибки Telegram и VK."""


class SendError(Exception):
    """Публикация не удалась. retryable — поможет ли повтор задачи через пару минут.

    Повтор помогает при сбоях сети и перегрузке площадки. Не помогает, когда
    нужна правка настроек: неверный токен, бот не админ, нет прав на стену.
    """

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable
