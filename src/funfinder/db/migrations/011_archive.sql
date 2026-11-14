-- Архив: прошедшее уходит из рабочих таблиц.
--
-- Пайплайну нужно только свежее: извлечение смотрит посты за 30 дней, склейка —
-- события той же даты, сайт — будущее и ещё 30 дней страницы «событие прошло».
-- DAG archive раз в сутки переносит сюда то, что старше 60 дней: рабочие
-- таблицы не растут, а история остаётся для аналитики. Правила — в
-- funfinder/ops/archive.py.
--
-- Столбцы — как у рабочих таблиц, плюс archived_at. Внешних ключей нет
-- намеренно: архив — снимок, и удаление площадки из справочника его не трогает.
-- Добавили столбец в рабочую таблицу — добавьте и в архивную. Иначе перенос
-- упадёт громко, а не потеряет данные молча, и тест в CI это заметит раньше.
CREATE SCHEMA archive;

-- LIKE без INCLUDING копирует только столбцы и NOT NULL: без значений по
-- умолчанию, identity и CHECK — в архив пишутся готовые строки.
CREATE TABLE archive.events (LIKE catalog.events);
ALTER TABLE archive.events
    ADD COLUMN archived_at timestamptz NOT NULL DEFAULT now(),
    ADD PRIMARY KEY (event_id);
CREATE INDEX archive_events_city_date_idx ON archive.events (city_id, event_date);

-- Упоминания: в рабочей схеме это staging.events.
CREATE TABLE archive.mentions (LIKE staging.events);
ALTER TABLE archive.mentions
    ADD COLUMN archived_at timestamptz NOT NULL DEFAULT now(),
    ADD PRIMARY KEY (id);
CREATE INDEX archive_mentions_event_idx ON archive.mentions (event_id);
CREATE INDEX archive_mentions_post_idx ON archive.mentions (source_id, post_id);

CREATE TABLE archive.vk_posts (LIKE raw.vk_posts);
ALTER TABLE archive.vk_posts
    ADD COLUMN archived_at timestamptz NOT NULL DEFAULT now(),
    ADD PRIMARY KEY (owner_id, post_id);
CREATE INDEX archive_vk_posts_source_published_idx ON archive.vk_posts (source_id, published_at);

CREATE TABLE archive.tg_posts (LIKE raw.tg_posts);
ALTER TABLE archive.tg_posts
    ADD COLUMN archived_at timestamptz NOT NULL DEFAULT now(),
    ADD PRIMARY KEY (source_id, post_id);
CREATE INDEX archive_tg_posts_source_published_idx ON archive.tg_posts (source_id, published_at);

CREATE TABLE archive.post_extractions (LIKE staging.post_extractions);
ALTER TABLE archive.post_extractions
    ADD COLUMN archived_at timestamptz NOT NULL DEFAULT now(),
    ADD PRIMARY KEY (source_id, post_id);

CREATE TABLE archive.site_events (LIKE raw.site_events);
ALTER TABLE archive.site_events
    ADD COLUMN archived_at timestamptz NOT NULL DEFAULT now(),
    ADD PRIMARY KEY (id);

CREATE TABLE archive.event_pages (LIKE catalog.event_pages);
ALTER TABLE archive.event_pages
    ADD COLUMN archived_at timestamptz NOT NULL DEFAULT now(),
    ADD PRIMARY KEY (city_id, slug);
