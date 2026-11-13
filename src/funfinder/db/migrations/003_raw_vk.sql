-- Посты со стен сообществ VK как есть. Форма таблицы повторяет источник;
-- общую для всех платформ форму данные получают в staging.
CREATE TABLE raw.vk_posts (
    owner_id      bigint NOT NULL,              -- отрицательный id сообщества
    post_id       bigint NOT NULL,              -- растёт со временем: по нему инкремент
    source_id     integer NOT NULL REFERENCES meta.sources,
    published_at  timestamptz NOT NULL,
    text          text NOT NULL DEFAULT '',     -- текст самого поста
    repost_text   text NOT NULL DEFAULT '',     -- текст репоста (copy_history), часто там и есть анонс
    url           text NOT NULL,                -- https://vk.com/wall-123_456
    is_pinned     boolean NOT NULL DEFAULT false,
    payload       jsonb NOT NULL,               -- ответ API без идентификаторов людей
    content_hash  text NOT NULL,                -- sha256 текста: по нему ловим правки
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    changed_at    timestamptz,                  -- когда последний раз поменялся текст
    ingest_run_id text,                         -- run_id Airflow: какой запуск записал строку
    PRIMARY KEY (owner_id, post_id)
);

CREATE INDEX vk_posts_source_published_idx ON raw.vk_posts (source_id, published_at DESC);

-- Для инкремента extract_events: «что появилось или изменилось после X».
CREATE INDEX vk_posts_last_change_idx ON raw.vk_posts ((coalesce(changed_at, loaded_at)));
