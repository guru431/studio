"""ВРЕМЕННО: снимки раздела видео для глаз (удаляется до коммита)."""
import httpx
import pytest

from fakes import video_object
from test_ui_browser import (CLIP, add_gateway, browser, go, live, png_of, put_file, ready_clips, READY,  # noqa: F401
                             VIDEO_FEED_READY, clips, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]
SHOTS = "C:/Users/SUPERU~1/AppData/Local/Temp/claude/c--AI-projects-studio/26de84ff-866f-44c0-a507-a286f8ff411f/scratchpad/shots"


@pytest.mark.parametrize("width,theme", [(390, "dark"), (1280, "light")])
def test_shots(browser, live, ready_clips, width, theme):
    login = f"shot{width}"
    live.add_user(login, theme=theme)
    running_id = f"video_{len(live.gw.video_ids) + 3:024x}"

    async def running(request):
        if request.method != "GET":
            return await live.gw.builtin(request)
        return httpx.Response(200, json=video_object(running_id, status="in_progress", progress=37,
                                                     detail="делается: шаг 3 из 8", seed=7))

    live.gw.on[f"/slot/videos/{running_id}"] = running
    busy = f"{CLIP}[data-state=in_progress]"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("video"),
        ("wait", VIDEO_FEED_READY),
        ("screenshot", f"{SHOTS}/video-empty-{width}-{theme}.png"),
        put_file("#video-file", png_of(400, 600, "purple")),
        ("wait", "document.querySelector('#video-source img')?.naturalWidth === 400"),
        ("type", "#video-prompt", "she slowly turns her head and smiles"),
        ("click", "#video-go"), ("wait", clips("done", 1)),
        ("click", "#chip-smooth"), ("click", "#video-go"), ("wait", clips("done", 2)),
        ("click", "#video-go"),
        ("wait", f"document.querySelector('{busy}')?.textContent.includes('37%')"),
        ("click", "#chip-quality"),
        ("screenshot", f"{SHOTS}/video-feed-{width}-{theme}.png"),
        ("press", "Escape"),
        ("click", f"{CLIP}[data-state=done] .card-delete"),
        ("wait", "document.querySelector('#video-delete-dialog')"),
        ("screenshot", f"{SHOTS}/video-delete-{width}-{theme}.png"),
        ("press", "Escape"),
        ("click", f"{busy} .card-cancel"), ("wait", clips("cancelled", 1)),
        ("screenshot", f"{SHOTS}/video-cancelled-{width}-{theme}.png")],
        width=width, height=900 if width == 1280 else 844, mobile=width == 390, timeout=20000)
    assert not r.get("failed"), r
    assert not r.get("log"), r["log"]
