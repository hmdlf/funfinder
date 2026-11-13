-- Города. Город, часовой пояс и язык заложены в модель с первого дня.
CREATE TABLE meta.cities (
    city_id     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]+$'),  -- часть URL: /lipetsk/
    name        text NOT NULL,
    timezone    text NOT NULL,                                       -- IANA: Europe/Moscow
    language    text NOT NULL DEFAULT 'ru',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Категории событий. Справочник для LLM-извлечения и витрин.
CREATE TABLE meta.categories (
    category_id smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]+$'),
    name_ru     text NOT NULL,
    sort_order  smallint NOT NULL DEFAULT 100
);

INSERT INTO meta.categories (slug, name_ru, sort_order) VALUES
    ('concert',    'Концерты',               10),
    ('theatre',    'Театр',                  20),
    ('exhibition', 'Выставки',               30),
    ('cinema',     'Кино',                   40),
    ('lecture',    'Лекции и встречи',       50),
    ('workshop',   'Мастер-классы',          60),
    ('party',      'Вечеринки',              70),
    ('festival',   'Фестивали и праздники',  80),
    ('sport',      'Спорт',                  90),
    ('walk',       'Прогулки и экскурсии',  100),
    ('boardgames', 'Настольные игры',       110),
    ('meetup',     'Сходки и клубы',        120),
    ('kids',       'Детям',                 130),
    ('other',      'Другое',                999);

-- Источники. Источник правды — файлы sources/*.yaml, их синхронизирует
-- ingest_vk (задача sync_sources). Здесь — то, что узнали при загрузке.
CREATE TABLE meta.sources (
    source_id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id     integer NOT NULL REFERENCES meta.cities,
    platform    text NOT NULL CHECK (platform IN ('vk', 'tg', 'site')),
    handle      text NOT NULL,      -- VK: короткое имя сообщества (lipetsk_afisha или club123)
    external_id bigint,             -- VK: числовой id сообщества, заполняет DAG
    title       text,               -- название, как его отдаёт API
    note        text,               -- комментарий из yaml
    settings    jsonb NOT NULL DEFAULT '{}',
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, handle)
);

CREATE INDEX sources_city_platform_idx ON meta.sources (city_id, platform) WHERE is_active;

-- Журнал загрузок: одна строка на (DAG, запуск, источник).
-- Отсюда DAG понимает, было ли что-то новое, а позже читают
-- source_health и дашборд мониторинга.
CREATE TABLE meta.ingest_log (
    dag_id      text NOT NULL,
    run_id      text NOT NULL,
    source_id   integer NOT NULL REFERENCES meta.sources,
    status      text NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    attempt     integer NOT NULL DEFAULT 1,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    pages       integer,
    fetched     integer,
    inserted    integer,
    updated     integer,
    error       text,
    PRIMARY KEY (dag_id, run_id, source_id)
);

CREATE INDEX ingest_log_source_idx ON meta.ingest_log (source_id, started_at DESC);
