-- Справочник площадок. Источник правды — sources/venues/<город>.yaml,
-- его переносит в базу ingest_vk (задача sync_sources) вместе с источниками.

CREATE TABLE meta.venues (
    venue_id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id    integer NOT NULL REFERENCES meta.cities,
    slug       text NOT NULL CHECK (slug ~ '^[a-z0-9-]+$'),  -- постоянный ключ: ссылки из источников, будущие URL
    name       text NOT NULL,                                -- как показывать на сайте
    address    text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (city_id, slug)
);

-- Варианты написания, как в yaml. Сравнение по основам слов делает код
-- (funfinder.events.venues): правила нормализации меняются без миграций.
CREATE TABLE meta.venue_aliases (
    venue_id integer NOT NULL REFERENCES meta.venues ON DELETE CASCADE,
    alias    text NOT NULL,
    PRIMARY KEY (venue_id, alias)
);

-- Площадка по умолчанию для паблика самой площадки: «малый зал» у Атмосферы.
ALTER TABLE meta.sources ADD COLUMN venue_id integer REFERENCES meta.venues ON DELETE SET NULL;

-- Привязка упоминаний и событий. Исходный текст места (venue) остаётся как был.
ALTER TABLE staging.events ADD COLUMN venue_id integer REFERENCES meta.venues ON DELETE SET NULL;
ALTER TABLE catalog.events ADD COLUMN venue_id integer REFERENCES meta.venues ON DELETE SET NULL;

-- Витрина: у привязанных событий название и адрес — из справочника.
-- Событие с известной площадкой показываем, даже если время не указано
-- (выставка в галерее). Новые столбцы — в конце: так требует CREATE OR REPLACE VIEW.
CREATE OR REPLACE VIEW catalog.events_public AS
SELECT e.event_id,
       c.slug                           AS city,
       e.title,
       e.event_date,
       e.event_time,
       coalesce(v.name, e.venue)        AS venue,
       e.price,
       e.category,
       e.status,
       count(*)                         AS mentions,
       array_agg(DISTINCT s.source_url) AS source_urls,
       v.slug                           AS venue_slug,
       v.address                        AS venue_address
  FROM catalog.events e
  JOIN meta.cities c ON c.city_id = e.city_id
  JOIN staging.events s ON s.event_id = e.event_id
  LEFT JOIN meta.venues v ON v.venue_id = e.venue_id
 WHERE (e.event_time IS NOT NULL OR e.venue IS NOT NULL OR e.venue_id IS NOT NULL)
   AND e.event_date >= (now() AT TIME ZONE c.timezone)::date
 GROUP BY e.event_id, c.slug, v.venue_id;

-- Места, которые не нашлись в справочнике: что в него добавить.
CREATE VIEW staging.unmatched_venues AS
SELECT s.venue,
       count(*)                                  AS mentions,
       array_agg(DISTINCT src.handle)            AS sources,
       max(s.published_at)                       AS last_seen
  FROM staging.events s
  JOIN meta.sources src USING (source_id)
 WHERE s.venue IS NOT NULL
   AND s.venue_id IS NULL
   AND s.quarantine_reason IS NULL
 GROUP BY s.venue;
