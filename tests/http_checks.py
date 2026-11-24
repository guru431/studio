"""Bulk HTTP assertions on the application's loop, with the current browser headers and cookies."""
import asyncio

import httpx


def on_http(studio, scenario):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=studio.c.app, client=("testclient", 50000)),
            base_url=str(studio.c.base_url), headers=studio.c.headers, cookies=studio.c.cookies.jar,
        ) as client:
            return await scenario(client)

    return studio.c.portal.call(run)


def wait_http_state(studio, url, states):
    """Poll the real endpoint without competing with the background job for every loop turn."""
    async def poll(client):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while True:
            response = await client.get(url)
            assert response.status_code == 200, response.text
            body = response.json()
            if body["state"] in states:
                return body
            if loop.time() >= deadline:
                raise AssertionError(f"{url} не дошёл до {states}: {body}")
            await asyncio.sleep(0.01)

    return on_http(studio, poll)
