"""Bounded project image preview, private paths and session checks."""
import pytest

import claude_code
from test_code import ALPHA, detail, setup, url
from test_media import png


def test_project_image_requires_pin_and_returns_verified_bytes(studio, caplog):
    aid, machine, _ = setup(studio, pin='2468', opened=False)
    data = png(160, 100)
    path = '.superpowers/тайное превью.png'
    machine.projects['alpha']['files'][path] = data
    endpoint = url(aid, 'projects', ALPHA, 'image')
    assert studio.c.post(endpoint, json={'path': path}).json()['detail']['code'] == 'closed'
    studio.c.post(url(aid, 'open'), json={'pin': '2468'})
    response = studio.c.post(endpoint, json={'path': path})
    assert response.status_code == 200 and response.content == data
    assert response.headers['content-type'] == 'image/png'
    assert response.headers['cache-control'] == 'no-store'
    assert path not in caplog.text


@pytest.mark.parametrize('data,status', [(b'<svg onload="alert(1)"/>', 415), (b'<html>private</html>', 415),
                                      (b'not an image', 415)])
def test_project_image_rejects_active_or_invalid_content(studio, data, status):
    aid, machine, _ = setup(studio)
    machine.projects['alpha']['files']['fake.png'] = data
    response = studio.c.post(url(aid, 'projects', ALPHA, 'image'), json={'path': 'fake.png'})
    assert response.status_code == status


def test_project_image_limits_and_revocation_during_decode(studio, monkeypatch):
    aid, machine, _ = setup(studio)
    endpoint = url(aid, 'projects', ALPHA, 'image')
    machine.projects['alpha']['files']['image.png'] = png(2, 2)
    monkeypatch.setattr(claude_code, 'RAW_MAX', 2)
    assert studio.c.post(endpoint, json={'path': 'image.png'}).status_code == 413
    monkeypatch.setattr(claude_code, 'RAW_MAX', 10 * 1024 * 1024)
    original = claude_code._image_bytes

    def decode(data):
        result = original(data)
        monkeypatch.setattr(claude_code, '_session_open', lambda digest: False)
        return result

    monkeypatch.setattr(claude_code, '_image_bytes', decode)
    response = studio.c.post(endpoint, json={'path': 'image.png'})
    assert response.status_code == 409 and detail(response)['code'] == 'closed'
