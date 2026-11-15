-- Публикация подборок «Завтра в <городе>» в каналы и сообщества.

-- Куда публиковать. Источник правды — раздел publish: в sources/<город>.yaml,
-- его переносит в базу sync_sources вместе с источниками. Убрали из yaml —
-- место становится неактивным, журнал выпусков остаётся.
CREATE TABLE meta.publish_targets (
    target_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id    integer NOT NULL REFERENCES meta.cities,
    platform   text NOT NULL CHECK (platform IN ('tg', 'vk')),
    handle     text NOT NULL,                  -- tg: @канал или числовой id (-100…); vk: id сообщества
    note       text,
    settings   jsonb NOT NULL DEFAULT '{}',    -- настройки подборки: какие категории не включать
    is_active  boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, handle)
);

-- Журнал выпусков: строка на (место, день подборки). По нему DAG publish не
-- публикует одну подборку дважды, даже если запуск повторили.
--
-- sending — отправка началась, исход неизвестен (процесс оборвался посреди
-- запроса). Такой выпуск автоматически не повторяется: сообщение могло уйти,
-- а дубль в канале хуже, чем пропуск. Решает человек — см. README.
CREATE TABLE meta.publish_log (
    target_id  integer NOT NULL REFERENCES meta.publish_targets,
    issue_date date NOT NULL,                  -- на какой день подборка
    status     text NOT NULL CHECK (status IN ('sending', 'sent', 'failed', 'skipped')),
    events     integer,                        -- сколько событий в тексте
    message_id text,                           -- id сообщения или поста на площадке
    text       text,                           -- что отправили: для разбора формата
    error      text,
    run_id     text,
    attempt    integer NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    sent_at    timestamptz,
    PRIMARY KEY (target_id, issue_date)
);
