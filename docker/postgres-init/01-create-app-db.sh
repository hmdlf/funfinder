#!/bin/bash
# Создаёт пользователя и базы проекта. Postgres выполняет скрипты из
# /docker-entrypoint-initdb.d только при первом старте с пустым томом.
# Без -u: если у файла нет флага исполнения (например, после распаковки
# zip на Windows), Postgres не запускает его, а подключает через source,
# и nounset сломал бы его собственный entrypoint.
set -eo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE USER "${FUNFINDER_DB_USER}" WITH PASSWORD '${FUNFINDER_DB_PASSWORD}';
    CREATE DATABASE funfinder OWNER "${FUNFINDER_DB_USER}";
    CREATE DATABASE funfinder_test OWNER "${FUNFINDER_DB_USER}";
EOSQL
