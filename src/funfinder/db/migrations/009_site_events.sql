-- Сайты площадок как источник: афиша театра — это уже готовые события
-- с датой и временем, LLM для них не нужна.
--
-- ingest_sites разбирает страницу афиши и пишет сюда по строке на показ.
-- process_events (задача load_site_events) переносит новые и изменённые строки
-- в staging.events как упоминания — дальше они идут тем же путём, что и посты:
-- проверка, площадка, склейка с анонсами из VK и Telegram.
CREATE TABLE raw.site_events (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- он же post_id упоминания в staging.events
    source_id     integer NOT NULL REFERENCES meta.sources,
    event_key     text NOT NULL,          -- ключ показа на сайте: «834@2026-11-14@11:00»
    title         text NOT NULL,
    event_date    date NOT NULL,
    event_time    time,
    url           text NOT NULL,          -- страница спектакля
    content_hash  text NOT NULL,          -- название, дата, время, ссылка: по нему ловим правки
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    changed_at    timestamptz,
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    removed_at    timestamptz,            -- показ пропал из афиши, хотя его месяц на странице есть: отменили или перенесли
    loaded_hash   text,                   -- что из этой строки уже лежит в staging.events
    ingest_run_id text,
    UNIQUE (source_id, event_key)
);

CREATE INDEX site_events_date_idx ON raw.site_events (event_date);
