-- Здоровье источников: что DAG source_health узнал о каждом источнике.
--
-- Строка на (день, источник). Повторная проверка в тот же день переписывает
-- строку дня, поэтому таблица — короткая история: с какого дня паблик молчит,
-- с какого дня падает загрузка. Правила — в funfinder/ops/health.py.
CREATE TABLE meta.source_health (
    checked_on      date NOT NULL,                -- день проверки по часовому поясу города
    source_id       integer NOT NULL REFERENCES meta.sources,
    status          text NOT NULL CHECK (status IN ('ok', 'warn', 'fail')),
    problems        text[] NOT NULL DEFAULT '{}', -- что не так, человеческим языком
    failures_in_row integer NOT NULL DEFAULT 0,   -- последние загрузки с ошибкой подряд
    last_success_at timestamptz,                  -- последняя успешная загрузка
    last_error      text,                         -- текст последней ошибки загрузки
    last_item_at    timestamptz,                  -- последний пост (VK, Telegram) или новый показ (сайт)
    silence_limit   interval,                     -- дольше этого молчать для источника необычно
    posts_30d       integer,                      -- постов за 30 дней, отправленных в LLM (у сайтов — null)
    mentions_30d    integer,                      -- событий из них, прошедших проверку
    upcoming        integer NOT NULL DEFAULT 0,   -- будущих событий каталога, где есть упоминание из источника
    checked_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (checked_on, source_id)
);

-- Последняя проверка каждого активного источника: SELECT * FROM meta.source_health_latest.
CREATE VIEW meta.source_health_latest AS
SELECT DISTINCT ON (h.source_id)
       h.checked_on,
       s.platform,
       s.handle,
       coalesce(nullif(s.note, ''), s.title) AS name,
       h.status,
       h.problems,
       h.failures_in_row,
       h.last_success_at,
       h.last_item_at,
       h.silence_limit,
       h.posts_30d,
       h.mentions_30d,
       h.upcoming,
       h.last_error,
       h.source_id
  FROM meta.source_health h
  JOIN meta.sources s USING (source_id)
 WHERE s.is_active
 ORDER BY h.source_id, h.checked_on DESC;
