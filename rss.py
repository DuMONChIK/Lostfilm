#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
from pathlib import Path
import re
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from yaml import load as yaml_load
from yaml import SafeLoader as yaml_loader
from xml.etree import ElementTree
import yaml
import datetime as dt
import sqlite3


class UniqueKeyLoader(yaml_loader):
    """Безопасный YAML-загрузчик, запрещающий повторяющиеся ключи."""

    def construct_mapping(self, node, deep=False):
        seen_keys = {}
        for key_node, _ in node.value:
            # Ключ слияния YAML (<<) обрабатывает родительский SafeLoader.
            if key_node.tag == 'tag:yaml.org,2002:merge':
                continue

            key = self.construct_object(key_node, deep=deep)
            try:
                if key in seen_keys:
                    raise yaml.constructor.ConstructorError(
                        'Первое объявление ключа',
                        seen_keys[key],
                        f'Обнаружен повторяющийся ключ {key!r}',
                        key_node.start_mark
                    )
                seen_keys[key] = key_node.start_mark
            except TypeError as error:
                raise yaml.constructor.ConstructorError(
                    'При разборе YAML-словаря',
                    node.start_mark,
                    'Обнаружен нехешируемый ключ',
                    key_node.start_mark
                ) from error

        return super().construct_mapping(node, deep=deep)


# var_dump analogue
def var_dump(var):
    print(f"{var=}, type={type(var)}")


# Парсинг настроек
config_path = Path(__file__).resolve().parent / 'config.yml'
try:
    with open(config_path, 'r', encoding='utf-8') as yaml_config:
        config = yaml_load(yaml_config, Loader=UniqueKeyLoader)
except yaml.YAMLError as error:
    print(f'Ошибка в {config_path.name}:\n{error}', file=sys.stderr)
    raise SystemExit(2) from None

# настройка логирования
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=config['verbose'].upper(),
    handlers=[
        RotatingFileHandler(
            Path(__file__).resolve().parent / 'rss.log',
            maxBytes=config['log']['maxBytes'],
            backupCount=config['log']['backupCount']
        ),
    ]
)


def compile_regex_subscriptions(regex_config):
    """Компилирует подписки из subscriptions-regex и проверяет их синтаксис."""
    if regex_config is None:
        return []
    if not isinstance(regex_config, dict):
        logging.error('Параметр subscriptions-regex должен быть словарём')
        raise SystemExit(2)

    compiled_subscriptions = []
    for pattern, quality in regex_config.items():
        try:
            compiled_subscriptions.append((re.compile(pattern), quality))
        except (TypeError, re.error) as error:
            logging.error(
                'Некорректное регулярное выражение в subscriptions-regex %r: %s',
                pattern,
                error
            )
            raise SystemExit(2) from error
    return compiled_subscriptions


subscriptions = config.get('subscriptions') or {}
regex_subscriptions = compile_regex_subscriptions(
    config.get('subscriptions-regex')
)


def is_subscribed(real_name, quality):
    """Проверяет точные подписки и подписки по регулярным выражениям."""
    if subscriptions.get(real_name) == quality:
        return True

    for pattern, subscription_quality in regex_subscriptions:
        if subscription_quality == quality and pattern.search(real_name):
            logging.debug(
                'Подписка по regex: pattern=%r, real_name=%r, quality=%r',
                pattern.pattern,
                real_name,
                quality
            )
            return True
    return False


# Cookie для авторизации на трекере
cookies = ';'.join(['{}={}'.format(cookie, config['auth'][cookie])
                   for cookie in config['auth']])

# Строка подключения к transmission RPC
transmission_url = 'http://{host}:{port}/transmission/rpc/'.format(
    host=config['transmission']['host'],
    port=config['transmission']['port'])


# Функция запроса transmission RPC
transmission_session_id = None


def transmission_rpc_request(rpc_data: dict) -> dict:
    global transmission_session_id
    for _ in range(2):
        torrent_request = requests.post(
            transmission_url,
            data=json.dumps(rpc_data),
            headers={'X-Transmission-Session-Id': transmission_session_id},
            auth=(config['transmission']['user'],
                  config['transmission']['password']),
            timeout=config['timeout']
        )
        if torrent_request.status_code == 200:
            break
        elif torrent_request.status_code == 401:
            logging.error('Не авторизован в Transmission')
            exit(401)
        torrent_session_search = re.search('X-Transmission-Session-Id: .+?(?=<)',
                                           torrent_request.text)
        if torrent_session_search:
            transmission_session_id = torrent_session_search.group(0).split(':')[1].strip()
    if torrent_request.status_code != 200:
        logging.error('transmission RPC: {}'.format(torrent_request))
        exit(torrent_request.status_code)
    response = json.loads(torrent_request.text)
    if response['result'] != 'success':
        logging.error('transmission RPC: {}'.format(response))
        exit(1)
    return response


# Запрос директории загрузки по-умолчанию
request_download_root = transmission_rpc_request(
    {
        'arguments': {
            'fields': ['download-dir']
        },
        'method': 'session-get'
    })

download_root = Path(request_download_root['arguments']['download-dir'])
logging.debug("Директория: {}".format(download_root))

# Формируем каталог уже загруженных файлов
request_available_torrents = transmission_rpc_request(
    {
        'arguments': {
            'fields': ['name']
        },
        'method': 'torrent-get'
    }
)

catalog = dict()
for job in request_available_torrents['arguments']['torrents']:
    if 'LostFilm.TV' not in job['name']:
        continue
    if ' - LostFilm.TV' in job['name']:
        name = ' '.join(job['name'].split(' - LostFilm.TV')[0].split()[:-1])
        series = 'S{:02d}E99'.format(
            int(job['name'].split(' - LostFilm.TV')[0].split()[-1]))
    else:
        data = job['name'].split('.rus.LostFilm.TV.')[0]
        #var_dump(data)
        series = data.split('.')[-2]
        quality = data.split('.')[-1]
        name = data.replace(quality, '').replace(
            series, '').strip('.').replace('.', ' ')
    # Обработка нестандартного именования серий
    if name in config['aliases']:
        name = config['aliases'][name]
    if name not in catalog:
        catalog.update({name: {series}})
    else:
        catalog[name].add(series)
logging.debug("Каталог: {}".format(catalog))

# Подключаемся к базе с историей загрузок
con = sqlite3.connect("/home/dima/Lostfilm/download-history.db")
cur = con.cursor()
cur.execute("""
        CREATE TABLE IF NOT EXISTS history(
          download_date TEXT,
          real_name TEXT,
          series TEXT,
          quality TEXT,
          title TEXT,
          link TEXT,
          PRIMARY KEY (real_name, series, quality)
        )
    """)

# Запрос RSS ленты
list_request = requests.get(
    config['url'],
    timeout=config['timeout'])
list_request.encoding = 'utf-8'
#print(list_request.text)
rss_items = ElementTree.fromstring(
    list_request.text).find('channel').findall('item')

for item in rss_items:
    title = item.find('title').text
    link = item.find('link').text
    quality = item.find('category').text.strip('[]')

    # Парсинг атрибутов раздачи
    search_real_name = re.search(r"\(.+\)\.", title)
    if search_real_name:
        real_name = search_real_name.group(0).strip('().')
        search_series = re.search(r"\(S[0-9]+E[0-9]+\)", title)
        if search_series:
            series = search_series.group(0).strip('()')
        else:
            logging.warning(f"Не смог найти серию: {title}")
            continue
    else:
        logging.warning(f"Не получилось найти имя: {title}")
        continue

    # Проверяем наличие в базе истории закачек
    db_row = cur.execute("""
        SELECT 1
        FROM history
        WHERE real_name = ?
          AND series = ?
          AND quality = ?
        LIMIT 1
    """, (real_name, series, quality)).fetchone()
    exists_db = db_row is not None

    exists_config = is_subscribed(real_name, quality)
    exists_blacklist = real_name in config['blacklist']
    exists_downloading = real_name in catalog and series in catalog[real_name]

#    logging.debug(
#        f'Проверка наличий real_name={real_name}, series={series}, quality={quality}, exists_db={exists_db} exists_blacklist={exists_blacklist}, exists_downloading={exists_downloading}')

    # Качаем только нужные серии
    if (
        exists_config
        and not exists_blacklist
        and (
            not series.endswith('E99') and
            not series.endswith('E999')
        )

        # И если их нет в списке текущих раздач в transmission
        and not exists_downloading

        # И если нет в истории закачек
        and not exists_db

    ):
        logging.info(f"Добавляем {title}")
        logging.debug(
            f'real_name={real_name}, series={series}, quality={quality}')

        db_data = [
            ( title, quality, series, real_name, link )
        ]
        cur.executemany("INSERT INTO history (download_date,title, quality, series, real_name, link)"
                        " VALUES (datetime('now'),?,?,?,?,?)", db_data)
        con.commit()  # Remember to commit the transaction after executing INSERT.

        download_location=''.join(real_name.strip('.').split(':'))

        transmission_rpc_request({
            'arguments': {
                'cookies': cookies,
                'filename': link,
                # Имя директории не может оканчиваться точкой
                'download-dir': str(download_root / download_location)
            },
            'method': 'torrent-add'
        })
    else:
        logging.debug(
#            f'Пропуск [blacklisted: {exists_blacklist}, downloaded: {exists_db}, downloading: {exists_downloading}] config={exists_config}, real_name={real_name}, series={series}, quality={quality}, ')
            f'Пропуск [already_downloaded={exists_db}, now_downloading={exists_downloading}] want_to_download={exists_config}, real_name={real_name}, series={series}, quality={quality}, ')
