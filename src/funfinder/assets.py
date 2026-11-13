"""Assets Airflow: «данные обновились» как сигнал для следующих DAG-ов.

ingest_vk публикует RAW_VK_POSTS, ingest_tg — RAW_TG_POSTS, когда в raw.vk_posts
или raw.tg_posts что-то добавилось или изменилось. process_events запускается
по любому из них, а не по часам.

process_events публикует CATALOG_EVENTS, когда каталог событий изменился.
По нему будут запускаться сборка сайта и публикация в каналы.
"""

from airflow.sdk import Asset

RAW_VK_POSTS = Asset(name="raw_vk_posts", uri="funfinder://raw/vk_posts")
RAW_TG_POSTS = Asset(name="raw_tg_posts", uri="funfinder://raw/tg_posts")
CATALOG_EVENTS = Asset(name="catalog_events", uri="funfinder://catalog/events")
