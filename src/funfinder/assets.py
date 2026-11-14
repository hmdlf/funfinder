"""Assets Airflow: «данные обновились» как сигнал для следующих DAG-ов.

ingest_vk публикует RAW_VK_POSTS, ingest_tg — RAW_TG_POSTS, ingest_sites —
RAW_SITE_EVENTS, когда в raw что-то добавилось или изменилось. process_events
запускается по любому из них, а не по часам.

process_events публикует CATALOG_EVENTS, когда каталог событий изменился.
По нему будут запускаться сборка сайта и публикация в каналы.
"""

from airflow.sdk import Asset

RAW_VK_POSTS = Asset(name="raw_vk_posts", uri="funfinder://raw/vk_posts")
RAW_TG_POSTS = Asset(name="raw_tg_posts", uri="funfinder://raw/tg_posts")
RAW_SITE_EVENTS = Asset(name="raw_site_events", uri="funfinder://raw/site_events")
CATALOG_EVENTS = Asset(name="catalog_events", uri="funfinder://catalog/events")
