"""Проверка расширений ACP OpenCode; содержимое остаётся только в памяти хода."""

import math
import re

from . import events, permissions

SESSION_ID = re.compile(r"ses_[A-Za-z0-9_-]{1,200}")


def child_meta(update):
    meta = update.get('_meta')
    return meta.get('opencode/child-session') if isinstance(meta, dict) else None


def child(update, root, known):
    """None — родитель; False — метка есть, но цепочка не доказана."""
    raw = child_meta(update)
    meta = update.get('_meta')
    if not isinstance(meta, dict) or 'opencode/child-session' not in meta:
        return None
    if not isinstance(raw, dict):
        return False
    sid, parent, depth = raw.get('id'), raw.get('parentID'), raw.get('depth')
    if (not isinstance(sid, str) or not SESSION_ID.fullmatch(sid) or sid == root
            or not isinstance(parent, str) or not SESSION_ID.fullmatch(parent)
            or type(depth) is not int or not 1 <= depth <= 16):
        return False
    expected = 1 if parent == root else known.get(parent, {}).get('depth', -2) + 1
    if depth != expected or sid == parent or sid not in known and len(known) >= 50:
        return False
    if sid in known and (known[sid]['parent'] != parent or known[sid]['depth'] != depth):
        return False
    return {'id': sid, 'parent': parent, 'depth': depth,
            'title': events.clip(str(raw.get('title') or 'Подзадача'), 200)}


def commands(update):
    rows = update.get('availableCommands')
    if not isinstance(rows, list):
        return []
    return [{'name': row['name'], 'description': events.clip(str(row.get('description') or ''), 300)}
            for row in rows[:100] if isinstance(row, dict) and isinstance(row.get('name'), str)
            and re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', row['name'])]


def usage(raw):
    if not isinstance(raw, dict):
        return None
    return permissions.token_usage({**raw, 'reasoningOutputTokens': raw.get('thoughtTokens')})


def context(update, previous=None):
    current = permissions.context(update.get('used'), update.get('size'))
    if current is None:
        return previous
    current = {**(previous or {}), **current}
    cost = update.get('cost')
    if (isinstance(cost, dict) and type(cost.get('amount')) in (int, float)
            and math.isfinite(cost['amount']) and cost['amount'] >= 0 and cost.get('currency') == 'USD'):
        current['cost'] = {'amount': cost['amount'], 'currency': 'USD'}
    return current


def status(update):
    """Не передаём response.body и тексты ошибок провайдера в состояние/журнал."""
    kind, meta = update.get('sessionUpdate'), update.get('_meta') or {}
    if not isinstance(meta, dict):
        return {}
    result = {}
    if 'opencode/retry' in meta:
        raw = meta['opencode/retry']
        retry = None
        if isinstance(raw, dict) and type(raw.get('attempt')) is int and 0 < raw['attempt'] < 10000:
            retry = {'attempt': raw['attempt'], 'next': str(raw.get('nextRetryAt') or '')[:64]}
            error = raw.get('error')
            if isinstance(error, dict) and type(error.get('status')) is int:
                retry['status'] = error['status']
        result['retry'] = retry
    raw = update if kind == 'compaction_update' else meta.get('opencode/compaction')
    if isinstance(raw, dict) and raw.get('status') in ('started', 'in_progress', 'completed', 'failed'):
        result['compaction'] = raw['status']
    return result
