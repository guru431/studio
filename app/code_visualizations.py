"""Ephemeral, session-bound HTML previews. No file content or paths are written to disk.

The document itself may load libraries from the CDN hosts below (the fragment chooses which). The studio adds only
its own copy of lucide (`/static/vendor/lucide.min.js`) and only when the fragment mentions lucide, so a preview
without external libraries makes no external request at all."""
import re
import secrets
import clock

TTL = 300
MAX_BYTES = 2 * 1024 * 1024
# One preview per user, as with the project PDF cache; token is only a lookup key.
VIEWS: dict[int, tuple] = {}
CDNS = ('https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://esm.sh '
        'https://unpkg.com https://persistent.oaistatic.com/plugins/visualize/')
FONTS = 'https://fonts.googleapis.com https://fonts.gstatic.com https://fonts.bunny.net'
LUCIDE = '/static/vendor/lucide.min.js'
USES_LUCIDE = re.compile(r'lucide', re.IGNORECASE)
# 'self' — the studio's own static copy of lucide; the sandboxed frame has no same-origin rights anyway.
CSP = (f"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' 'self' {CDNS}; "
       f"style-src 'unsafe-inline' {CDNS} {FONTS}; img-src data: blob: {CDNS}; "
       f"font-src data: {CDNS} {FONTS}; media-src data: blob:; connect-src data: blob:; "
       "worker-src blob:; frame-src 'none'; object-src 'none'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'self'; sandbox allow-scripts")


def forget(user_id: int, session: str) -> None:
    kept = VIEWS.get(user_id)
    if kept and kept[1] == session:
        del VIEWS[user_id]


def expire() -> None:
    now = clock.NOW()
    for user_id, kept in list(VIEWS.items()):
        if kept[-1] <= now:
            del VIEWS[user_id]


def keep(user_id: int, session: str, aid: int, pid: str, text: str) -> str:
    expire()
    token = secrets.token_hex(16)
    VIEWS[user_id] = (token, session, aid, pid, text, clock.NOW() + TTL)
    return token


def document(fragment: str) -> str:
    # The same font/color variables used by Codex fragments; all styles stay in the frame.
    icons = (f'\n<script src="{LUCIDE}"></script>\n'
             '<script>globalThis.lucide?.createIcons({attrs:{width:16,height:16}});</script>'
             if USES_LUCIDE.search(fragment) else '')
    return ('''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Визуализация</title>
<style>:root{color-scheme:light dark;--color-background-primary:light-dark(#fff,#202020);
--color-background-secondary:light-dark(#f4f4f4,#292929);--color-background-tertiary:light-dark(#eee,#333);
--color-text-primary:light-dark(#202020,#eee);--color-text-secondary:light-dark(#666,#aaa);
--color-text-tertiary:light-dark(#888,#888);--color-border-primary:light-dark(#ccc,#555);
--color-border-secondary:light-dark(#ddd,#444);--color-border-tertiary:light-dark(#eee,#333);
--font-sans:system-ui,sans-serif;--font-mono:ui-monospace,monospace;
--border-radius-md:8px;--border-radius-lg:12px;--border-radius-xl:16px}
*{box-sizing:border-box}body{margin:0;padding:12px;font:14px var(--font-sans);
color:var(--color-text-primary);background:var(--color-background-primary)}
button,input,select,textarea{font:inherit}svg{max-width:100%}</style>
</head><body>''' + fragment + icons + '''
</body></html>''')
