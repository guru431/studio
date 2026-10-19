"""Studio photo submission in a real browser; local API stub, no GPU or credentials."""
import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from conftest import APP
from browser import launch
from image_upload import decode_form

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    yield from launch(tmp_path_factory.mktemp("studio-browser"))


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("selected_model", ["lustify", "flux"])
def test_photo_preview_submission_and_clear(browser, width, selected_model):
    next_model = "flux" if selected_model == "lustify" else "lustify"
    output = io.BytesIO()
    Image.new("RGB", (40, 30), "blue").save(output, "PNG")
    png = output.getvalue()
    page = (APP / "index.html").read_bytes()
    received = []
    rendered = {}

    class Handler(BaseHTTPRequestHandler):
        def reply(self, body, mime="application/json", status=200):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/studio/":
                self.reply(page, "text/html; charset=utf-8")
            elif self.path.endswith("/image"):
                self.reply(rendered[self.path.split("/")[-2]], "image/png")
            elif self.path.startswith("/studio/api/jobs/job"):
                self.reply(json.dumps({"id": self.path.rsplit("/", 1)[-1], "state": "done", "model": "lustify",
                                       "seed": 7, "elapsed": 1}).encode())
            elif self.path.endswith("/current"):
                self.reply(b"{}", status=404)
            else:
                self.reply(b"", status=204)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            extra = {}
            fields, photo = decode_form(raw, self.headers["Content-Type"], extra_files=extra)
            received.append((fields, photo, extra))
            job_id = f"job{len(received)}"
            result = io.BytesIO()
            Image.new("RGB", (40, 30), ["blue", "green", "red"][len(received) - 1]).save(result, "PNG")
            rendered[job_id] = result.getvalue()
            self.reply(json.dumps({"id": job_id, "seed": 7}).encode(), status=202)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    b64 = base64.b64encode(png).decode()
    setup = """(() => {
      const bytes = Uint8Array.from(atob('%s'), x => x.charCodeAt(0));
      const transfer = new DataTransfer();
      transfer.items.add(new File([bytes], 'photo.png', {type:'image/png'}));
      document.querySelector('#image').files = transfer.files;
      document.querySelector('#image').dispatchEvent(new Event('change'));
      document.querySelector('#prompt').value = 'watercolor landscape';
      document.querySelector('#strength').value = '0.4';
      return true;
    })()""" % b64
    try:
        result = browser.drive(f"http://127.0.0.1:{server.server_port}/studio/", [
            ("idle",), ("eval", "setup", setup),
            ("wait", "document.querySelector('#input-preview').naturalWidth === 40"),
            ("click", "#go"), ("wait", "document.querySelectorAll('#gallery figure').length === 1"),
            ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth"),
            ("click", "#clear-image"),
            ("eval", "cleared", "document.querySelector('#input-preview').hidden && document.querySelector('#image').files.length === 0"),
            ("eval", "selectModel", f"document.querySelector('#model').value = '{selected_model}'"),
            ("click", "#gallery figure:first-child .edit-photo"),
            ("wait", "document.querySelector('#editor').open"),
            ("eval", "defaultModel", "document.querySelector('#edit-model-label').textContent"),
            ("type", "#edit-prompt", "change only this patch to green"),
            ("click", "#edit-local"), ("click", "#edit-submit"),
            ("eval", "emptyMask", "document.querySelector('#editor').open && document.querySelector('#edit-error').textContent.length > 0"),
            ("click", "#edit-mask"),
            ("click", "#edit-submit"),
            ("wait", "!document.querySelector('#editor').open && document.querySelectorAll('#gallery figure').length === 2"),
            ("eval", "selectNext", f"document.querySelector('#model').value = '{next_model}'"),
            ("click", "#gallery figure:first-child .edit-photo"),
            ("wait", "document.querySelector('#editor').open"),
            ("eval", "reset", "!document.querySelector('#edit-local').checked && document.querySelector('#edit-prompt').value === ''"),
            ("eval", "nextLabel", "document.querySelector('#edit-model-label').textContent"),
            ("eval", "strengthVisible", "!document.querySelector('#edit-strength-label').hidden"),
            ("type", "#edit-prompt", "make the landscape a watercolor"),
            ("click", "#edit-submit"),
            ("wait", "!document.querySelector('#editor').open && document.querySelectorAll('#gallery figure').length === 3"),
            ("eval", "editorOverflow", "document.documentElement.scrollWidth > innerWidth"),
        ], width=width, height=900, mobile=width == 390)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)
    assert not result.get("failed"), result
    assert not result["values"]["overflow"] and result["values"]["cleared"]
    assert len(received) == 3 and received[0][0]["model"] == "lustify"
    assert received[0][0]["strength"] == "0.4" and received[0][1] == png
    names = {"lustify": "Lustify", "flux": "FLUX"}
    assert names[selected_model] in result["values"]["defaultModel"] and result["values"]["reset"]
    assert names[next_model] in result["values"]["nextLabel"]
    assert result["values"]["strengthVisible"] == (next_model == "lustify")
    assert not result["values"]["editorOverflow"]
    assert result["values"]["emptyMask"]
    assert received[1][0]["size"] == "original" and received[1][0]["model"] == selected_model
    assert received[1][1] == rendered["job1"] and "mask" in received[1][2]
    assert received[2][1] == rendered["job2"] and not received[2][2]
    assert received[2][0]["model"] == next_model
    assert received[1 if selected_model == "lustify" else 2][0]["strength"] == "0.25"
    assert "strength" not in received[1 if selected_model == "flux" else 2][0]
    assert not [v for v in result.get("log", []) if "исключение:" in v or "console.error:" in v]


@pytest.mark.parametrize("width,selected_model", [(390, "lustify"), (1280, "flux")])
def test_description_can_be_edited_and_regenerated_without_source_photo(browser, width, selected_model):
    output = io.BytesIO()
    Image.new("RGB", (64, 48), "blue").save(output, "PNG")
    source = output.getvalue()
    output = io.BytesIO()
    Image.new("RGB", (64, 48), "green").save(output, "PNG")
    generated = output.getvalue()
    page = (APP / "index.html").read_bytes()
    description = "An exceptionally detailed landscape with precise lighting and textures. " * 100
    described, requests = [], []

    class Handler(BaseHTTPRequestHandler):
        def reply(self, body, mime="application/json", status=200):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/studio/":
                self.reply(page, "text/html; charset=utf-8")
            elif self.path.endswith("/current"):
                self.reply(b"{}", status=404)
            elif self.path.endswith("/description"):
                self.reply(json.dumps({"text": description, "truncated": False, "model": "gemma-abl"}).encode())
            elif self.path.endswith("/image"):
                self.reply(generated, "image/png")
            elif self.path.startswith("/studio/api/jobs/"):
                is_description = self.path.rsplit("/", 1)[-1].startswith("d")
                self.reply(json.dumps({"id": self.path.rsplit("/", 1)[-1], "state": "done",
                                       "kind": "description" if is_description else "image",
                                       "model": "gemma-abl" if is_description else selected_model,
                                       "seed": 7, "elapsed": 1}).encode())
            else:
                self.reply(b"", status=204)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path.endswith("/descriptions"):
                _, photo = decode_form(raw, self.headers["Content-Type"])
                described.append(photo)
                self.reply(json.dumps({"id": f"d{len(described)}", "kind": "description"}).encode(), status=202)
            else:
                assert self.headers["Content-Type"] == "application/json"
                requests.append(json.loads(raw))
                self.reply(b'{"id":"g1","seed":7}', status=202)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    setup = f"""(() => {{
      const bytes=Uint8Array.from(atob('{base64.b64encode(source).decode()}'), x=>x.charCodeAt(0));
      const files=new DataTransfer(); files.items.add(new File([bytes], 'source.png', {{type:'image/png'}}));
      document.querySelector('#image').files=files.files;
      document.querySelector('#image').dispatchEvent(new Event('change'));
      document.querySelector('#model').value='{selected_model}'; return true;
    }})()"""
    try:
        result = browser.drive(f"http://127.0.0.1:{server.server_port}/studio/", [
            ("idle",), ("eval", "setup", setup), ("click", "#describe-input"),
            ("wait", "document.querySelector('#description-dialog').open"),
            ("eval", "descriptionLength", "document.querySelector('#description-text').value.length"),
            ("eval", "append", "(() => { const text=document.querySelector('#description-text'); text.value += ' Visible extra detail.'; text.dispatchEvent(new Event('input')); return true; })()"),
            ("type", "#description-extra", "Добавь маленькую оранжевую лодку"),
            ("click", "#description-generate"),
            ("wait", "!document.querySelector('#description-dialog').open && document.querySelectorAll('#gallery figure').length === 1"),
            ("click", "#gallery .describe-photo"),
            ("wait", "document.querySelector('#description-dialog').open"),
            ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth && document.querySelector('#description-dialog').scrollWidth <= document.querySelector('#description-dialog').clientWidth"),
        ], width=width, height=900, mobile=width == 390)
    finally:
        server.shutdown(); server.server_close(); thread.join(1)
    assert not result.get("failed"), result
    assert result["values"]["descriptionLength"] == len(description)
    assert result["values"]["fits"]
    assert described == [source, generated]
    assert len(requests) == 1 and requests[0]["model"] == selected_model
    assert requests[0]["prompt"].startswith(description.rstrip())
    assert "Visible extra detail." in requests[0]["prompt"] and "Добавь маленькую оранжевую лодку" in requests[0]["prompt"]
    assert "image" not in requests[0] and "mask" not in requests[0]
    assert not [v for v in result.get("log", []) if "исключение:" in v or "console.error:" in v]


@pytest.mark.parametrize("width", [390, 1280])
def test_gallery_image_is_animated_and_the_ready_video_plays(browser, width):
    """«Оживить» у картинки ленты кладёт её PNG в раздел видео, «Оживить» раздела шлёт её в
    api/videos, готовый ролик в списке — плеер и «Скачать» (спека 2026-10-19-srvllm-studio-video)."""
    output = io.BytesIO()
    Image.new("RGB", (40, 30), "green").save(output, "PNG")
    generated = output.getvalue()
    page = (APP / "index.html").read_bytes()
    video_id = "video_" + "ab" * 12
    sent = []

    class Handler(BaseHTTPRequestHandler):
        def reply(self, body, mime="application/json", status=200):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/studio/":
                self.reply(page, "text/html; charset=utf-8")
            elif self.path.endswith("/current"):
                self.reply(b"{}", status=404)
            elif self.path == "/studio/api/videos":
                done = [{"id": video_id, "object": "video", "status": "completed", "progress": 100,
                         "created_at": 1_800_000_000, "completed_at": 1_800_000_125, "seconds": "7",
                         "quality": "high", "smooth": False, "seed": 42, "prompt_source": "auto",
                         "detail": "готово", "error": None}]
                self.reply(json.dumps({"object": "list", "data": done if sent else [], "has_more": False}).encode())
            elif "/content" in self.path:
                self.reply(b"\0" * 64, "video/mp4")
            elif self.path.endswith("/image"):
                self.reply(generated, "image/png")
            elif self.path.startswith("/studio/api/jobs/"):
                self.reply(json.dumps({"id": "job1", "state": "done", "kind": "image", "model": "lustify",
                                       "seed": 7, "elapsed": 1}).encode())
            else:
                self.reply(b"", status=204)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/studio/api/videos":
                sent.append(decode_form(raw, self.headers["Content-Type"]))
                self.reply(json.dumps({"id": video_id, "status": "queued", "detail": "в очереди: перед ним 0"}).encode())
            else:
                self.reply(b'{"id":"job1","seed":7}', status=202)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    setup = """(() => {
      document.querySelector('#prompt').value = 'a garden';
      document.querySelector('#video-seconds').value = '7';
      document.querySelector('#video-quality').value = 'high';
      document.querySelector('#video-smooth').checked = false;
      return true;
    })()"""
    try:
        result = browser.drive(f"http://127.0.0.1:{server.server_port}/studio/", [
            ("idle",), ("eval", "setup", setup), ("click", "#go"),
            ("wait", "document.querySelectorAll('#gallery figure').length === 1"),
            ("click", "#gallery figure:first-child .animate-photo"),
            ("wait", "!document.querySelector('#video-preview').hidden && document.querySelector('#video-preview').naturalWidth === 40"),
            ("type", "#video-prompt", "she slowly turns her head"),
            ("click", "#video-go"),
            ("wait", "document.querySelector('#videos video') !== null"),
            ("eval", "src", "document.querySelector('#videos video').getAttribute('src')"),
            ("eval", "inline", "document.querySelector('#videos video').playsInline && document.querySelector('#videos video').controls"),
            ("eval", "download", "document.querySelector('#videos .actions a').getAttribute('href') + ' ' + document.querySelector('#videos .actions a').textContent"),
            ("eval", "item", "document.querySelector('#videos .video-item').textContent"),
            ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth"),
        ], width=width, height=900, mobile=width == 390)
    finally:
        server.shutdown(); server.server_close(); thread.join(1)
    assert not result.get("failed"), result
    values = result["values"]
    assert len(sent) == 1
    fields, image = sent[0]
    assert image == generated
    assert fields == {"prompt": "she slowly turns her head", "seconds": "7", "quality": "high", "smooth": "false"}
    assert values["src"] == f"api/videos/{video_id}/content" and values["inline"]
    assert values["download"] == f"api/videos/{video_id}/content?download=1 Скачать"
    for words in ("Готов", "2:05", "7 с", "720p", "16 кадров/с", "придуман по фото", "seed 42", "Удалить"):
        assert words in values["item"], words
    assert not values["overflow"]
    assert not [v for v in result.get("log", []) if "исключение:" in v or "console.error:" in v]
