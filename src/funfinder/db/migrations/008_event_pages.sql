-- Страницы отдельных событий: постоянные адреса для поисковиков.
--
-- Номер события в catalog.events не годится для адреса: при пересборке
-- каталога события создаются заново и номера меняются. Поэтому опубликованные
-- адреса хранятся здесь, а сборка сайта каждый раз заново находит, какому
-- событию какой адрес принадлежит: по городу, дате и похожему названию.
CREATE TABLE catalog.event_pages (
    city_id      integer NOT NULL REFERENCES meta.cities,
    slug         text NOT NULL CHECK (slug ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9-]+$'),  -- 2026-11-12-prestizh
    event_date   date NOT NULL,
    title        text NOT NULL,     -- последнее известное: для страницы «событие прошло»
    title_norm   text NOT NULL,     -- по нему адрес находит своё событие в следующей сборке
    venue        text,
    venue_slug   text,
    source_url   text,
    redirect_to  text,              -- адрес основной страницы, если событие склеилось с другим
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),  -- последняя сборка, где событие было в афише
    PRIMARY KEY (city_id, slug)
);

CREATE INDEX event_pages_city_date_idx ON catalog.event_pages (city_id, event_date);
