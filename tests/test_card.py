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
