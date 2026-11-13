-- Слои данных. Подробности — в README, раздел «Модель данных».
CREATE SCHEMA IF NOT EXISTS meta;        -- справочники, конфигурация, служебные журналы
CREATE SCHEMA IF NOT EXISTS raw;         -- данные источников как есть
CREATE SCHEMA IF NOT EXISTS staging;     -- извлечённые кандидаты в события
CREATE SCHEMA IF NOT EXISTS catalog;     -- проверенные события без дублей
CREATE SCHEMA IF NOT EXISTS quarantine;  -- не прошедшие проверки качества
