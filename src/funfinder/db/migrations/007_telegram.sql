-- Telegram как второй источник и общий для платформ ключ поста.
--
-- До этой миграции извлечение было привязано к VK: журнал
-- staging.vk_post_extractions и упоминания staging.events ссылались на пост
-- через owner_id сообщества VK. Теперь пост любой платформы — это
-- (source_id, post_id): у VK и у Telegram id поста уникален внутри источника.

-- Посты публичных каналов Telegram из веб-версии t.me/s/<канал>. Форма
-- повторяет источник, как у raw.vk_posts.
CREATE TABLE raw.tg_posts (
    source_id     integer NOT NULL REFERENCES meta.sources,
    post_id       bigint NOT NULL,              -- растёт со временем: по нему инкремент
    channel       text NOT NULL,                -- имя канала: sinitsabar
    published_at  timestamptz NOT NULL,
    text          text NOT NULL DEFAULT '',
    reply_text    text NOT NULL DEFAULT '',     -- цитата поста, на который это ответ: «сегодня в 20:00» — а что именно
    media_count   integer NOT NULL DEFAULT 0,   -- фото и видео: текст с картинок пока не читаем
    url           text NOT NULL,                -- https://t.me/sinitsabar/2671
    content_hash  text NOT NULL,                -- sha256 текста: по нему ловим правки
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    changed_at    timestamptz,
    ingest_run_id text,
    PRIMARY KEY (source_id, post_id)
);

CREATE INDEX tg_posts_published_idx ON raw.tg_posts (published_at);
CREATE INDEX vk_posts_source_post_idx ON raw.vk_posts (source_id, post_id);

-- Общий вид постов всех платформ: отсюда читает extract_events.
-- quoted_text — текст, без которого пост непонятен: репост в VK, цитата ответа в Telegram.
CREATE VIEW raw.posts AS
SELECT 'vk'::text AS platform, source_id, post_id, url, published_at, text, repost_text AS quoted_text, content_hash
  FROM raw.vk_posts
UNION ALL
SELECT 'tg', source_id, post_id, url, published_at, text, reply_text, content_hash
  FROM raw.tg_posts;

-- Представление карантина ссылается на журнал через owner_id — пересоздадим ниже.
DROP VIEW quarantine.events;

-- Журнал извлечения: общий для платформ.
ALTER TABLE staging.vk_post_extractions RENAME TO post_extractions;
ALTER TABLE staging.post_extractions
    RENAME CONSTRAINT vk_post_extractions_status_check TO post_extractions_status_check;
ALTER TABLE staging.post_extractions ADD COLUMN source_id integer REFERENCES meta.sources;
UPDATE staging.post_extractions x
   SET source_id = r.source_id
  FROM raw.vk_posts r
 WHERE r.owner_id = x.owner_id AND r.post_id = x.post_id;
-- Вместе со столбцом уходят старый первичный ключ и внешний ключ на raw.vk_posts.
ALTER TABLE staging.post_extractions DROP COLUMN owner_id;
ALTER TABLE staging.post_extractions
    ALTER COLUMN source_id SET NOT NULL,
    ADD PRIMARY KEY (source_id, post_id);

-- Упоминания: у них и так есть source_id, owner_id VK больше не нужен.
DROP INDEX staging.staging_events_post_idx;
ALTER TABLE staging.events DROP COLUMN owner_id;
CREATE INDEX staging_events_post_idx ON staging.events (source_id, post_id);

CREATE VIEW quarantine.events AS
SELECT 'event'::text       AS level,
       s.source_id,
       s.source_url,
       s.title,
       s.quarantine_reason AS reason,
       s.created_at        AS at
  FROM staging.events s
 WHERE s.quarantine_reason IS NOT NULL
UNION ALL
SELECT 'post',
       r.source_id,
       r.url,
       NULL,
       x.status,
       x.processed_at
  FROM staging.post_extractions x
  JOIN raw.posts r USING (source_id, post_id)
 WHERE x.status IN ('refused', 'invalid_json');
