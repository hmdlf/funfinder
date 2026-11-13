-- Извлечённые события и каталог без дублей. Заполняет DAG process_events:
-- extract_events → staging, validate_quality → карантин, dedupe_events → catalog.

-- Категорий не хватало под реальные анонсы Липецка: квизы — заметная доля
-- постов, а регулярные занятия (группы, кружки) надо отличать от разовых событий.
INSERT INTO meta.categories (slug, name_ru, sort_order) VALUES
    ('quiz',    'Квизы',            115),
    ('classes', 'Занятия и кружки', 125)
ON CONFLICT (slug) DO NOTHING;

-- Уникальные события. Поля пересчитываются из всех упоминаний (staging.events).
CREATE TABLE catalog.events (
    event_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id    integer NOT NULL REFERENCES meta.cities,
    title      text NOT NULL,
    title_norm text NOT NULL,                     -- название для сравнения при склейке
    event_date date NOT NULL,
    event_time time,
    venue      text,
    price      text,
    category   text NOT NULL REFERENCES meta.categories (slug),
    status     text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'cancelled', 'postponed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX events_city_date_idx ON catalog.events (city_id, event_date);

-- Журнал извлечения: какой пост и какую его версию уже прогнали через LLM.
-- content_hash тот же, что в raw.vk_posts: изменился текст — пост прогоняется заново.
CREATE TABLE staging.vk_post_extractions (
    owner_id       bigint NOT NULL,
    post_id        bigint NOT NULL,
    content_hash   text NOT NULL,
    status         text NOT NULL CHECK (status IN ('ok', 'skipped_empty', 'refused', 'invalid_json')),
    model          text,
    prompt_version text NOT NULL,
    tokens_in      integer NOT NULL DEFAULT 0,
    tokens_out     integer NOT NULL DEFAULT 0,
    raw_answer     text,                          -- ответ модели как есть: для разбора ошибок
    processed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, post_id),
    FOREIGN KEY (owner_id, post_id) REFERENCES raw.vk_posts ON DELETE CASCADE
);

-- Упоминания: каждое событие из каждого поста, с повторами. Общая для всех
-- платформ форма: ссылка на источник вместо полей конкретной платформы.
CREATE TABLE staging.events (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id         integer NOT NULL REFERENCES meta.sources,
    city_id           integer NOT NULL REFERENCES meta.cities,
    owner_id          bigint NOT NULL,
    post_id           bigint NOT NULL,
    source_url        text NOT NULL,              -- ссылка на пост: её покажем рядом с событием
    published_at      timestamptz NOT NULL,
    title             text,
    event_date        date,
    event_time        time,
    venue             text,
    price             text,
    category          text,
    status            text NOT NULL DEFAULT 'scheduled',
    validated         boolean NOT NULL DEFAULT false,
    quarantine_reason text,                       -- null — прошло проверку
    event_id          bigint REFERENCES catalog.events,  -- null — ещё не склеено
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX staging_events_post_idx ON staging.events (owner_id, post_id);
CREATE INDEX staging_events_event_idx ON staging.events (event_id);

-- Витрина: будущие события, у которых есть хотя бы время или место,
-- со ссылками на все посты-источники. Отсюда читают сайт, каналы и бот.
CREATE VIEW catalog.events_public AS
SELECT e.event_id,
       c.slug                           AS city,
       e.title,
       e.event_date,
       e.event_time,
       e.venue,
       e.price,
       e.category,
       e.status,
       count(*)                         AS mentions,
       array_agg(DISTINCT s.source_url) AS source_urls
  FROM catalog.events e
  JOIN meta.cities c ON c.city_id = e.city_id
  JOIN staging.events s ON s.event_id = e.event_id
 WHERE (e.event_time IS NOT NULL OR e.venue IS NOT NULL)
   AND e.event_date >= (now() AT TIME ZONE c.timezone)::date
 GROUP BY e.event_id, c.slug;

-- Карантин одним взглядом: отбракованные события и посты целиком.
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
  FROM staging.vk_post_extractions x
  JOIN raw.vk_posts r USING (owner_id, post_id)
 WHERE x.status IN ('refused', 'invalid_json');
