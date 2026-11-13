# Короткие команды. Без make (например, на Windows) — те же команды
# из README, раздел «Команды».

.PHONY: env up down logs ps test lint psql migrate trigger shell

env:        ## создать .env с секретами
	python scripts/init_env.py

up:         ## собрать образ и поднять всё
	docker compose up -d --build

down:       ## остановить (данные в базе сохраняются)
	docker compose down

logs:       ## логи планировщика и задач
	docker compose logs -f airflow-scheduler

ps:
	docker compose ps

test:       ## тесты внутри того же образа, что и DAG-и
	docker compose run --rm airflow-cli python -m pytest tests -q -p no:cacheprovider

psql:       ## консоль базы проекта
	docker compose exec postgres psql -U funfinder -d funfinder

migrate:    ## применить новые миграции без перезапуска
	docker compose run --rm funfinder-migrate

trigger:    ## запустить ingest_vk вручную
	docker compose run --rm airflow-cli airflow dags trigger ingest_vk

shell:
	docker compose run --rm airflow-cli bash
