"""Сайты площадок: разбор афиш без LLM. Сайт → модуль с функцией collect(get, today).

Чтобы подключить новый сайт: модуль с разбором в этой папке, строка в PARSERS
и запись в разделе sites: файла sources/<город>.yaml (handle — домен).
"""

from funfinder.sites import ocknt, teatr48, teatr_tolstogo

PARSERS = {
    "teatr-tolstogo.ru": teatr_tolstogo.collect,
    "teatr48.ru": teatr48.collect,
    "ocknt.ru": ocknt.collect,
}
