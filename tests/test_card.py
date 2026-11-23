"""Очередь карты (app/card.py): кто из заданий студии держит карту шлюза, кто ждёт и сколько примерно осталось."""
from types import SimpleNamespace

import card
import chat
import connections
import images
import ocr
import speeches
import state
import transcripts
import translate
from conftest import T0, add_connection, add_gateway
from test_jobs import me, speech, transcript


def test_waiting_jobs_see_the_holder_estimate_and_their_place(studio):
    """Распознавание держит карту; картинка у шлюза, озвучка и ответ чата после 503 ждут — по порядку начала; облако
    не карта. Оценка — медиана недавних готовых (очередь учётки не в счёт). Студия ничего не держит — «другой клиент»,
    а ролику в очереди шлюза без держащего — ничего. «В работе» показывает то же, без чужой учётки и содержания."""
    uid = me(studio)
    gw = connections.get(uid, add_gateway(studio))
    cloud = connections.get(uid, add_connection(studio, name="Облако"))
    transcript(uid, "h1", "done", created=T0 - 1000, finished=T0 - 700)    # 300 с
    transcript(uid, "h2", "done", created=T0 - 1000, finished=T0 - 400)    # ждала h1: 300 с, не 600
    transcript(uid, "h3", "done", created=T0 - 2000, finished=T0 - 1500)   # 500 с
    assert card.typical("transcript", None) == 300
    transcript(uid, "t1")
    transcripts.JOBS["t1"] = SimpleNamespace(stage="распознаю", stage_at=T0 - 60, conn=gw, task=None)
    images.JOBS["i1"] = SimpleNamespace(id="i1", user_id=uid, state="working", finished_at=None, started=T0 - 50,
                                        conn=gw, kind="generation", model="snofs", task=None, detail="жду карту или генерирую")
    speech(uid, "s1")
    speeches.JOBS["s1"] = SimpleNamespace(stage="жду карту · озвучиваю 1 из 2", stage_at=T0 - 30, conn=gw, task=None)
    ocr.JOBS["o1"] = SimpleNamespace(stage="жду карту", stage_at=T0 - 40, conn=cloud)
    chat.STREAMS["m1"] = SimpleNamespace(card_wait=(T0 - 10, T0 - 10), conn=gw, model="local-gemma-abl")
    try:
        held = {"busy": "transcript", "minutes": 4}
        assert card.status() == {("photo", "i1"): held | {"ahead": 0}, ("speech", "s1"): held | {"ahead": 1},
                                 ("chat", "m1"): held | {"ahead": 2}}
        running = {i["id"]: i for i in studio.c.get("/api/jobs").json()["running"]}
        assert running["s1"]["card"] == held | {"ahead": 1} and "card" not in running["t1"]
        del transcripts.JOBS["t1"], images.JOBS["i1"]
        state.DB.execute("INSERT INTO videos(id, user_id, connection_id, source_media_id, prompt, seconds, quality,"
                         " smooth, state, progress, detail, created) VALUES ('v1', ?, ?, 'm', 'p', 5, 'standard', 1,"
                         " 'queued', 0, 'в очереди', ?)", (uid, gw.id, T0 - 20))
        other = {"busy": None, "minutes": None}
        assert card.status() == {("speech", "s1"): other | {"ahead": 0}, ("chat", "m1"): other | {"ahead": 2}}
    finally:
        chat.STREAMS.pop("m1", None)


def test_one_request_builds_the_queue_once_and_the_estimate_lives_a_minute(studio, monkeypatch):
    """Опрос «В работе» с несколькими ждущими строками считает очередь один раз; медиана вида и модели живёт
    `card.TYPICAL_SEC`: новый готовый её не меняет до срока."""
    uid = me(studio)
    gw = connections.get(uid, add_gateway(studio))
    transcript(uid, "h1", "done", created=T0 - 1000, finished=T0 - 700)    # 300 с
    transcript(uid, "t1")
    transcripts.JOBS["t1"] = SimpleNamespace(stage="распознаю", stage_at=T0 - 60, conn=gw, task=None)
    for n, sid in enumerate(("s1", "s2")):
        speech(uid, sid)
        speeches.JOBS[sid] = SimpleNamespace(stage="жду карту", stage_at=T0 - 30 + n, conn=gw, task=None)
    built = []
    status = card.status
    monkeypatch.setattr(card, "status", lambda: built.append(1) or status())
    running = {i["id"]: i for i in studio.c.get("/api/jobs").json()["running"]}
    assert [running[s]["card"]["ahead"] for s in ("s1", "s2")] == [0, 1] and len(built) == 1
    assert running["s1"]["card"]["minutes"] == 4
    transcript(uid, "h2", "done", created=T0 - 1000, finished=T0 - 100)    # ещё 600 с: медиана стала бы 450
    assert card.typical("transcript", None) == 300
    studio.clock[0] += card.TYPICAL_SEC
    assert card.typical("transcript", None) == 450


def test_cloud_roles_and_a_translation_awaiting_its_scan_do_not_hold_the_card(studio):
    """Облачная роль srvllm (ответ чата, перевод) карту не держит: ждущему — «другой клиент». Перевод файла, что ждёт
    свой скан, — тоже: карту держит распознавание, его оценка и вид."""
    uid = me(studio)
    gw = connections.get(uid, add_gateway(studio))
    speech(uid, "s1")
    speeches.JOBS["s1"] = SimpleNamespace(stage="жду карту · озвучиваю 1 из 2", stage_at=T0 - 30, conn=gw, task=None)
    chat.STREAMS["m1"] = SimpleNamespace(card_wait=None, conn=gw, model="gpt-cloud-role")
    translate.JOBS["t1"] = SimpleNamespace(stage="перевожу · 1 из 3", stage_at=T0 - 90, conn=gw, model="gpt-cloud-role")
    try:
        assert card.status() == {("speech", "s1"): {"busy": None, "minutes": None, "ahead": 0}}
        translate.JOBS["t1"] = SimpleNamespace(stage=f"{translate.SCANNING} 1 из 3", stage_at=T0 - 90, conn=gw,
                                               model="local-gemma-abl")
        ocr.JOBS["o1"] = SimpleNamespace(stage="распознаю · стр. 1 из 3", stage_at=T0 - 60, conn=gw)
        assert card.status() == {("speech", "s1"): {"busy": "scan", "minutes": None, "ahead": 0}}
    finally:
        chat.STREAMS.pop("m1", None)
        translate.JOBS.pop("t1", None)
        ocr.JOBS.pop("o1", None)
