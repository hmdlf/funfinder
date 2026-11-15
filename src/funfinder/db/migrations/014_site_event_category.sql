-- Категория показа, если сайт её указывает сам. У театров категория одна на
-- сайт (settings.category в yaml), а у ОЦКНТ в одной афише театр, концерты, кино
-- и мастер-классы — категория берётся из тега карточки. Пусто — категория сайта.
ALTER TABLE raw.site_events ADD COLUMN category text;
ALTER TABLE archive.site_events ADD COLUMN category text;  -- архив повторяет столбцы рабочей таблицы
