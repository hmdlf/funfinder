"""Assets Airflow: «данные обновились» как сигнал для следующих DAG-ов.

ingest_vk публикует RAW_VK_POSTS, когда в raw.vk_posts что-то добавилось
или изменилось. process_events запускается по этому событию, а не по часам.

process_events публикует CATALOG_EVENTS, когда каталог событий изменился.
По нему будут запускаться сборка сайта и публикация в каналы.
"""

from airflow.sdk import Asset

RAW_VK_POSTS = Asset(name="raw_vk_posts", uri="funfinder://raw/vk_posts")
CATALOG_EVENTS = Asset(name="catalog_events", uri="funfinder://catalog/events")
