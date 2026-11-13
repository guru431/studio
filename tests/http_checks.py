"""Bulk HTTP assertions on the application's loop, with the current browser headers and cookies."""
import httpx


def on_http(studio, scenario):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=studio.c.app, client=("testclient", 50000)),
            base_url=str(studio.c.base_url), headers=studio.c.headers, cookies=studio.c.cookies.jar,
        ) as client:
            return await scenario(client)

    return studio.c.portal.call(run)
