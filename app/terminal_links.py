"""Build launch links only from the two bounded addresses a Linux agent can advertise."""
import ipaddress
import re


_HOST = r'(?P<host>\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+)'
_PORT = r'(?P<port>[0-9]{1,5})'
_SSH = re.compile(r'ssh://(?:(?P<user>[A-Za-z0-9_][A-Za-z0-9_.-]{0,63})@)?' + _HOST + ':' + _PORT)
_WEB = re.compile('https://' + _HOST + '(?::' + _PORT + r')?(?P<path>/[A-Za-z0-9._~/-]*)?')


def _parse(value, pattern):
    if not isinstance(value, str) or len(value) > 1024 or not (match := pattern.fullmatch(value)):
        raise ValueError('address')
    name = match['host']
    if name.startswith('['):
        name = str(ipaddress.IPv6Address(name[1:-1]))
        authority = f'[{name}]'
    else:
        name = name.lower()
        try:
            name = str(ipaddress.IPv4Address(name))
        except ValueError:
            labels = name.split('.')
            if (len(name) > 253 or re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', labels[-1])
                    or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', v) for v in labels)):
                raise ValueError('host') from None
        authority = name
    port = int(match['port']) if match['port'] else None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('port')
    return match, name, authority, port


def clean(data):
    ssh_uri = command = web = None
    if data.get('ssh_uri') is not None:
        match, host, authority, port = _parse(data['ssh_uri'], _SSH)
        user = match['user'] + '@' if match['user'] else ''
        ssh_uri = f'ssh://{user}{authority}:{port}'
        command = f'ssh -p {port} {user}{host}'
    if data.get('web_url') is not None:
        match, _, authority, port = _parse(data['web_url'], _WEB)
        web = 'https://' + authority + (f':{port}' if port else '') + (match['path'] or '/')
    return {'ssh_uri': ssh_uri, 'ssh_command': command, 'web_url': web}
