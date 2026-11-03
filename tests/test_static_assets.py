"""Версия остаётся неизменной при выкладке; приватные/неизвестные пути недоступны."""
import pytest
import asyncio
from starlette.exceptions import HTTPException

from static_assets import Snapshot


async def check_snapshot(tmp_path):
    (tmp_path/'js').mkdir()
    script=tmp_path/'js/app.js'
    script.write_bytes(b'old release')
    snapshot=Snapshot(tmp_path)
    script.write_bytes(b'new release')
    get=await snapshot.get_response('js/app.js',{'method':'GET','headers':[]})
    assert get.body==b'old release' and get.headers['content-length']=='11'
    assert 'immutable' in get.headers['cache-control']
    head=await snapshot.get_response('js/app.js',{'method':'HEAD','headers':[]})
    assert head.body==b'' and head.headers['content-length']==get.headers['content-length']
    unchanged=await snapshot.get_response('js/app.js',{'method':'GET',
        'headers':[(b'if-none-match',get.headers['etag'].encode())]})
    assert unchanged.status_code==304
    for path in ('../private.env','missing.js'):
        with pytest.raises(HTTPException) as error:
            await snapshot.get_response(path,{'method':'GET','headers':[]})
        assert error.value.status_code==404


def test_snapshot_does_not_mix_releases_and_supports_conditional_head(tmp_path):
    asyncio.run(check_snapshot(tmp_path))
