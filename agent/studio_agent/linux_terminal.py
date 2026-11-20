"""Connection addresses chosen on this Linux computer. The bridge never opens a connection."""
import ipaddress
import json
import re
from urllib.parse import urlsplit

from .atomic import write_atomic

DEFAULT = {'host': '', 'user': '', 'port': 22, 'web_url': ''}


def _hostname(host):
    if not isinstance(host, str) or not 1 <= len(host) <= 253 or '%' in host:
        raise ValueError('Неверный адрес компьютера')
    try:
        address = ipaddress.ip_address(host)
        return f'[{address}]' if address.version == 6 else str(address)
    except ValueError:
        if (all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', s)
                for s in host.split('.')) and not re.fullmatch(r'[0-9]+|0x[0-9a-f]+', host.split('.')[-1], re.I)):
            return host.lower()
        raise ValueError('Неверный адрес компьютера') from None


def addresses(data):
    host, user, port, web = (data[k] for k in DEFAULT)
    ssh = None
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Порт SSH должен быть от 1 до 65535')
    if not isinstance(user, str) or user and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', user):
        raise ValueError('Неверное имя пользователя SSH')
    if host != '':
        ssh = f"ssh://{user + '@' if user else ''}{_hostname(host)}:{port}"
    elif user:
        raise ValueError('Укажите адрес компьютера для SSH')
    if web != '':
        if not isinstance(web, str) or len(web) > 1024 or not re.fullmatch(
                r'https://(?:[A-Za-z0-9.-]+|\[[A-Fa-f0-9:]+\])(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?', web):
            raise ValueError('Введите HTTPS-адрес консоли без пароля, параметров и фрагмента')
        parsed = urlsplit(web)
        _hostname(parsed.hostname)
        if parsed.port == 0:
            raise ValueError('Недопустимый порт веб-консоли')
    return {'ssh_uri': ssh, 'web_url': web or None}


def load(home):
    try:
        with (home / 'terminal.json').open('rb') as f:
            raw = f.read(4097)
    except FileNotFoundError:
        return DEFAULT.copy()
    if len(raw) > 4096:
        raise ValueError('Файл настроек терминала слишком большой')
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != set(DEFAULT):
        raise ValueError('Проверьте настройки терминала в меню моста')
    addresses(data)
    return data


def save(home, data):
    if not isinstance(data, dict) or set(data) != set(DEFAULT):
        raise ValueError('Неизвестные параметры терминала')
    addresses(data)
    write_atomic(home / 'terminal.json', json.dumps(data, ensure_ascii=True).encode('ascii'))


def read(home):
    return addresses(load(home))
