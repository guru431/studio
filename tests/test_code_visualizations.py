"""Private HTML previews: session-bound tickets, expiry and the sandbox response contract."""
import bridge
import claude_code
import code_visualizations
from test_media import png
from test_code import ALPHA, detail, setup, url
from conftest import signin


def ready(studio, **kwargs):
    aid, machine, link = setup(studio, **kwargs)
    machine.visualizations = True
    link.hello['visualizations'] = True
    machine.projects['alpha']['files']['view.html'] = b'<button onclick="this.textContent=123">Run</button>'
    return aid, machine, url(aid, 'projects', ALPHA, 'visualization')


def test_html_preview_is_private_bounded_and_sandboxed(studio, caplog):
    aid, machine, endpoint = ready(studio, pin='2468', opened=False)
    assert detail(studio.c.post(endpoint, json={'source': 'view.html'}))['code'] == 'closed'
    studio.c.post(url(aid, 'open'), json={'pin': '2468'})
    response = studio.c.post(endpoint, json={'source': 'view.html'})
    assert response.status_code == 200
    frame = studio.c.get(endpoint + '/' + response.json()['token'])
    assert frame.status_code == 200 and '<button onclick=' in frame.text
    assert frame.headers['cache-control'] == 'no-store'
    assert frame.headers['referrer-policy'] == 'no-referrer'
    csp = frame.headers['content-security-policy']
    assert 'sandbox allow-scripts' in csp and 'allow-same-origin' not in csp
    assert "connect-src data: blob:" in csp and "form-action 'none'" in csp
    assert 'view.html' not in caplog.text
    # Документ без lucide ничего не грузит сам: ни своей копии, ни CDN.
    assert '<script src=' not in frame.text and 'unpkg' not in frame.text


def test_lucide_comes_from_the_studio_and_only_when_the_fragment_uses_it(studio):
    aid, machine, endpoint = ready(studio)
    machine.projects['alpha']['files']['icons.html'] = b'<i data-lucide="check"></i>'
    token = studio.c.post(endpoint, json={'source': 'icons.html'}).json()['token']
    frame = studio.c.get(endpoint + '/' + token)
    assert f'<script src="{code_visualizations.LUCIDE}"></script>' in frame.text and 'unpkg' not in frame.text
    assert "'self'" in frame.headers['content-security-policy'].split('script-src')[1].split(';')[0]
    studio.c.post('/logout')
    script = studio.c.get(code_visualizations.LUCIDE)            # статика открыта и фрейму без cookie
    assert script.status_code == 200 and b'lucide 1.17.0' in script.content[:100]


def test_ticket_expires_is_bound_to_session_and_rechecks_computer(studio):
    aid, machine, endpoint = ready(studio)
    token = studio.c.post(endpoint, json={'source': 'view.html'}).json()['token']
    frame_url = endpoint + '/' + token
    assert studio.c.get(frame_url).status_code == 200
    machine.open_sessions.clear()
    assert detail(studio.c.get(frame_url))['code'] == 'closed'
    studio.c.post(url(aid, 'open'), json={'pin': None})
    studio.clock[0] += code_visualizations.TTL + 1
    assert studio.c.get(frame_url).status_code == 404
    token = studio.c.post(endpoint, json={'source': 'view.html'}).json()['token']
    signin(studio, 'owner')
    assert studio.c.get(endpoint + '/' + token).status_code == 404
    studio.c.post('/logout')
    assert studio.c.get(endpoint + '/' + token).status_code == 401
    kept = code_visualizations.VIEWS[1]
    code_visualizations.forget(1, kept[1])
    assert not code_visualizations.VIEWS


def test_old_bridge_and_oversized_or_revoked_results_are_rejected(studio, monkeypatch):
    aid, machine, endpoint = ready(studio)
    bridge.LINKS[aid].hello['visualizations'] = False
    assert detail(studio.c.post(endpoint, json={'source': 'view.html'}))['code'] == 'unsupported'
    bridge.LINKS[aid].hello['visualizations'] = True
    monkeypatch.setattr(code_visualizations, 'MAX_BYTES', 2)
    assert studio.c.post(endpoint, json={'source': 'view.html'}).status_code == 413
    monkeypatch.setattr(code_visualizations, 'MAX_BYTES', 2 * 1024 * 1024)
    original = machine._op_visualization

    def revoke(req, events):
        result = original(req, events)
        monkeypatch.setattr(claude_code, '_session_open', lambda digest: False)
        return result

    monkeypatch.setattr(machine, '_op_visualization', revoke)
    assert detail(studio.c.post(endpoint, json={'source': 'view.html'}))['code'] == 'closed'


def test_generated_raster_preview_uses_image_limits_and_validates_bytes(studio):
    aid, machine, endpoint = ready(studio)
    source = 'C:/Users/owner/.codex/generated_images/thread/view.png'
    machine.projects['alpha']['files'][source] = png(160, 100) + b'\0' * (code_visualizations.MAX_BYTES + 1)
    assert detail(studio.c.post(endpoint, json={'source': source}))['code'] == 'unsupported'
    machine.visualization_images = True
    bridge.LINKS[aid].hello['visualization_images'] = True
    response = studio.c.post(endpoint, json={'source': source})
    assert response.status_code == 200
    preview = studio.c.get(endpoint + '/' + response.json()['token'])
    assert preview.status_code == 200 and 'data:image/png;base64,' in preview.text
    assert 'generated_images' not in preview.text and 'sandbox allow-scripts' in preview.headers['content-security-policy']
    machine.projects['alpha']['files'][source] = b'not an image'
    assert studio.c.post(endpoint, json={'source': source}).status_code == 415
    machine.projects['alpha']['files'][source] = b'x' * (claude_code.IMAGE_MAX + 1)
    assert studio.c.post(endpoint, json={'source': source}).status_code == 413
