-- Токены пользователя для публикации от имени сообщества (VK ID).
--
-- Стену сообщества VK можно писать только с ключом пользователя-админа:
-- ключ сообщества и сервисный ключ для wall.post не подходят. Ключ VK ID
-- живёт час, refresh-токен — полгода, и при каждом обновлении VK выдаёт новую
-- пару, а старый refresh-токен перестаёт действовать. Поэтому пара хранится
-- в базе, а не в .env: её переписывает каждый запуск publish.
--
-- Сами токены зашифрованы (Fernet, ключ — AIRFLOW_FERNET_KEY из .env):
-- дамп базы без .env их не раскрывает. Получить первую пару —
-- python -m funfinder.publish.vk_auth, см. README, «Подборка: VK».
CREATE TABLE meta.oauth_tokens (
    provider          text PRIMARY KEY,           -- 'vk_id'
    client_id         text NOT NULL,              -- приложение VK ID
    device_id         text NOT NULL,              -- выдаёт VK ID при входе, нужен для обновления
    user_id           bigint,                     -- кто вошёл: админ сообщества
    scope             text NOT NULL DEFAULT '',   -- какие права выдал VK, через пробел
    access_token      text NOT NULL,              -- зашифровано
    refresh_token     text NOT NULL,              -- зашифровано
    access_expires_at timestamptz NOT NULL,
    authorized_at     timestamptz NOT NULL DEFAULT now(),  -- вход через браузер
    refreshed_at      timestamptz,                         -- последнее обновление пары
    updated_at        timestamptz NOT NULL DEFAULT now()
);
