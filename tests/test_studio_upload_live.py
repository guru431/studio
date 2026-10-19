"""Manual production acceptance: synthetic photo through API, studio and local PS client.

Requires GATEWAY_KEY_OWNER_IMAGES and an authorized deployment. Generates three images
under the slots controller, then releases the model. No user photos are read.
"""
import io
import base64
import os
import shutil
import subprocess
import time

import httpx
import pytest
from PIL import Image, ImageChops, ImageDraw

from conftest import CLIENT

pytestmark = [pytest.mark.manual, pytest.mark.timeout(1500)]
MODEL = "lustify-v10-krea2-turbo"
PROMPT = "a simple watercolor landscape, green hills, a blue lake, no people"


def test_describe_with_gemma_abl_then_generate_from_text(tmp_path):
    token = os.environ.get("GATEWAY_KEY_OWNER_IMAGES")
    if not token:
        pytest.skip("GATEWAY_KEY_OWNER_IMAGES is required")
    studio = os.environ.get("STUDIO_LIVE_URL", "https://llm.example.com/studio")
    source = Image.new("RGB", (768, 768), "skyblue")
    draw = ImageDraw.Draw(source)
    draw.ellipse((550, 40, 680, 170), fill="yellow")
    draw.polygon([(0, 768), (0, 440), (240, 260), (480, 440), (768, 320), (768, 768)], fill="forestgreen")
    draw.rectangle((300, 420, 480, 600), fill="red")
    draw.polygon([(260, 420), (390, 310), (520, 420)], fill="brown")
    draw.rectangle((355, 495, 410, 600), fill="white")
    source = source.resize((1440, 1440))
    image = io.BytesIO()
    source.save(image, "PNG")

    def wait_job(client, job_id):
        deadline = time.monotonic() + 1300
        while time.monotonic() < deadline:
            status = client.get(studio + "/api/jobs/" + job_id).json()
            if status["state"] in ("done", "error", "cancelled"):
                assert status["state"] == "done", status
                return status
            time.sleep(1)
        pytest.fail("job did not finish")

    with httpx.Client(timeout=30, trust_env=False) as client:
        assert client.post(studio + "/login", data={"token": token}).status_code == 303
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if client.get(studio + "/api/jobs/current").status_code == 404:
                break
            time.sleep(2)
        else:
            pytest.fail("Studio busy; owner jobs were not interrupted")
        try:
            response = client.post(studio + "/api/descriptions", files={"image": ("source.png", image.getvalue(), "image/png")})
            assert response.status_code == 202, response.text[:500]
            job_id = response.json()["id"]
            assert wait_job(client, job_id)["model"] == "gemma-abl"
            description = client.get(studio + "/api/jobs/" + job_id + "/description").json()
            assert description["model"] == "gemma-abl" and len(description["text"]) >= 300
            (tmp_path / "description.txt").write_text(description["text"], encoding="utf-8")
            prompt = description["text"] + "\n\nAdditional instructions: add a small blue boat in the foreground."
            assert len(prompt) <= 24000
            response = client.post(studio + "/api/jobs", json={"model": "lustify", "size": "square",
                                   "seed": 23, "prompt": prompt})
            assert response.status_code == 202, response.text[:500]
            job_id = response.json()["id"]
            assert wait_job(client, job_id)["model"] == "lustify"
            result = client.get(studio + "/api/jobs/" + job_id + "/image")
            assert result.status_code == 200
            with Image.open(io.BytesIO(result.content)) as generated:
                assert generated.size == (1024, 1024)
        finally:
            client.post(studio + "/api/release")


def test_masked_flux_edit_preserves_every_pixel_outside_selection():
    token = os.environ.get("GATEWAY_KEY_OWNER_IMAGES")
    if not token:
        pytest.skip("GATEWAY_KEY_OWNER_IMAGES is required")
    studio = os.environ.get("STUDIO_LIVE_URL", "https://llm.example.com/studio")
    source = Image.new("RGB", (1024, 1024), "white")
    ImageDraw.Draw(source).rectangle((350, 350, 674, 674), fill="red")
    area = (280, 280, 744, 744)
    mask = Image.new("L", source.size)
    mask.paste(255, area)

    def png(image):
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue()

    with httpx.Client(timeout=30, trust_env=False) as client:
        assert client.post(studio + "/login", data={"token": token}).status_code == 303
        page = client.get(studio + "/")
        assert page.status_code == 200 and 'id="editor"' in page.text
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if client.get(studio + "/api/jobs/current").status_code == 404:
                release = client.post(studio + "/api/release")
                results = release.json().get("results", {})
                if release.status_code == 200 and len(results) == 3 and all(
                        item["state"] == "released" for item in results.values()):
                    break
            time.sleep(2)
        else:
            pytest.fail("Studio or image API remained busy; owner jobs were not interrupted")
        try:
            response = client.post(studio + "/api/jobs", data={"model": "flux", "size": "original",
                                   "prompt": "Change the red square to a blue square. Keep everything else unchanged.",
                                   "seed": "21"},
                                   files={"image": ("source.png", png(source), "image/png"),
                                          "mask": ("mask.png", png(mask), "image/png")})
            assert response.status_code == 202, response.text[:500]
            job_id = response.json()["id"]
            deadline = time.monotonic() + 1300
            while time.monotonic() < deadline:
                status = client.get(studio + "/api/jobs/" + job_id).json()
                if status["state"] in ("done", "error", "cancelled"):
                    break
                time.sleep(1)
            assert status["state"] == "done", status
            image = client.get(studio + "/api/jobs/" + job_id + "/image")
            assert image.status_code == 200
            with Image.open(io.BytesIO(image.content)) as result:
                assert result.size == source.size
                difference = ImageChops.difference(result.convert("RGB"), source)
                assert difference.crop(area).getbbox() is not None, "model returned the original unchanged"
                difference.paste((0, 0, 0), area)
                assert difference.getbbox() is None, "pixels outside the mask changed"
        finally:
            client.post(studio + "/api/release")


def test_photo_api_maximum_size():
    token = os.environ.get("GATEWAY_KEY_OWNER_IMAGES")
    if not token:
        pytest.skip("GATEWAY_KEY_OWNER_IMAGES is required")
    source = io.BytesIO()
    Image.new("RGB", (512, 512), "forestgreen").save(source, "PNG")
    base = os.environ.get("STUDIO_LIVE_GATEWAY", "http://10.77.1.15:4000/slot")
    headers = {"Authorization": "Bearer " + token}
    with httpx.Client(timeout=1300, trust_env=False) as client:
        try:
            response = client.post(base + "/images/edits", headers=headers,
                                   data={"model": MODEL, "prompt": PROMPT, "strength": "0.65",
                                         "size": "1536x1536", "response_format": "b64_json", "seed": "20"},
                                   files={"image": ("source.png", source.getvalue(), "image/png")})
            assert response.status_code == 200, response.text[:500]
            with Image.open(io.BytesIO(base64.b64decode(response.json()["data"][0]["b64_json"]))) as result:
                assert result.size == (1440, 1440)
        finally:
            client.post(base + "/images/release", json={"model": MODEL}, headers=headers, timeout=60)


def test_photo_through_all_live_clients(tmp_path):
    token = os.environ.get("GATEWAY_KEY_OWNER_IMAGES")
    if not token:
        pytest.skip("GATEWAY_KEY_OWNER_IMAGES is required")
    ps = shutil.which("powershell") or shutil.which("pwsh")
    local = CLIENT / "studio-batch.local.ps1"
    if not ps or not local.exists():
        pytest.skip("PowerShell and studio-batch.local.ps1 are required")
    source = Image.new("RGB", (512, 512), "skyblue")
    draw = ImageDraw.Draw(source)
    draw.polygon([(0, 512), (0, 300), (160, 190), (320, 330), (512, 210), (512, 512)], fill="forestgreen")
    output = io.BytesIO()
    source.save(output, "PNG")
    photo = tmp_path / "source.png"
    photo.write_bytes(output.getvalue())
    fields = {"model": MODEL, "prompt": PROMPT, "strength": "0.4", "seed": "17",
              "size": "1024x1024", "response_format": "b64_json"}
    base = os.environ.get("STUDIO_LIVE_GATEWAY", "http://10.77.1.15:4000/slot")
    studio = os.environ.get("STUDIO_LIVE_URL", "https://llm.example.com/studio")
    with httpx.Client(timeout=1300, trust_env=False) as client:
        try:
            response = client.post(base + "/images/edits", data=fields,
                                   files={"image": ("source.png", output.getvalue(), "image/png")},
                                   headers={"Authorization": "Bearer " + token})
            assert response.status_code == 200, response.text[:500]
            with Image.open(io.BytesIO(base64.b64decode(response.json()["data"][0]["b64_json"]))) as result:
                assert result.size == (1024, 1024)

            response = client.post(studio + "/login", data={"token": token})
            assert response.status_code == 303, response.status_code
            # The owner can use studio while acceptance runs. Never cancel or retrieve their job.
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                current = client.get(studio + "/api/jobs/current")
                if current.status_code == 404:
                    response = client.post(studio + "/api/jobs", data={"model": "lustify", "size": "square",
                                           "prompt": PROMPT, "strength": "0.4", "seed": "18"},
                                           files={"image": ("source.png", output.getvalue(), "image/png")})
                    if response.status_code != 409:
                        break
                else:
                    assert current.status_code == 200, current.status_code
                time.sleep(2)
            else:
                pytest.fail("Studio remained busy for 300 seconds; no owner job was interrupted")
            assert response.status_code == 202, response.text[:500]
            job_id = response.json()["id"]
            deadline = time.monotonic() + 1300
            while time.monotonic() < deadline:
                status = client.get(studio + "/api/jobs/" + job_id).json()
                if status["state"] in ("done", "error", "cancelled"):
                    break
                time.sleep(1)
            assert status["state"] == "done", status
            result = client.get(studio + "/api/jobs/" + job_id + "/image")
            assert result.status_code == 200
            with Image.open(io.BytesIO(result.content)) as image:
                assert image.size == (1024, 1024)

            prompts = tmp_path / "prompts.txt"
            prompts.write_text(PROMPT + "\n", encoding="utf-8")
            out = tmp_path / "out"
            run = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                  "-File", str(local), "-PromptsFile", str(prompts), "-ImagePath", str(photo),
                                  "-Strength", "0.4", "-Seed", "19", "-Size", "square",
                                  "-OutDir", str(out), "-Url", base], capture_output=True, timeout=1350)
            assert run.returncode == 0, run.stderr.decode(errors="replace")
            results = list(out.glob("*.png"))
            assert len(results) == 1, run.stdout.decode(errors="replace")
            with Image.open(results[0]) as image:
                assert image.size == (1024, 1024)
        finally:
            client.post(base + "/images/release", json={"model": MODEL},
                        headers={"Authorization": "Bearer " + token}, timeout=60)
