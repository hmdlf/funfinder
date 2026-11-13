# Образ Airflow с зависимостями проекта.
# Версию Airflow меняем здесь и только здесь.
FROM apache/airflow:3.3.2

COPY requirements.txt /requirements.txt
# apache-airflow==${AIRFLOW_VERSION} в той же команде не даёт pip
# случайно обновить или откатить сам Airflow.
RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" -r /requirements.txt
