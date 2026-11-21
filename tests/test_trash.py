"""Корзина на 14 дней (app/trash.py, шаг схемы 58): удаление переносит запись в корзину — она уходит из лент,
поиска, «В работе», push, ссылок «Поделиться» и выгрузки; «Вернуть» возвращает всё, уборка через
`trash.TRASH_SEC` удаляет окончательно. Виды записей — `trash.KINDS`: новый вид без строки в MAKE роняет тесты."""
import io
import json
import zipfile
from dataclasses import dataclass, field
from typing import Callable

import pytest

import auth
import chat
import media
import search
import state
import trash
from conftest import T0, make_user, signin
from test_chat_shares import create as create_share, regular as regular_chat, unlock


def save(uid: int, data: bytes, mime: str = "audio/mpeg", name: str = "file.mp3") -> str:
    """Файл на «шаре» и строка media (как test_takeout.save)."""
    media_id, rel = media._plan(uid, mime)
    full = state.CFG.media_dir / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(data)
    state.DB.execute("INSERT INTO media(id, user_id, rel_path, mime, size, has_thumb, download_name, created)"
                     " VALUES (?, ?, ?, ?, ?, 0, ?, ?)", (media_id, uid, rel, mime, len(data), name, T0))
    return media_id


def new_chat(uid: int, chat_id: str, word: str) -> str:
    """Диалог с вопросом, ответом и отправленным документом (файл — на «шаре»); id файла."""
    state.DB.execute("INSERT INTO chats(id, user_id, title, model, created, updated) VALUES (?, ?, ?, 'm', ?, ?)",
                     (chat_id, uid, f"{word} title", T0, T0))
    state.DB.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                     " VALUES (?, ?, 'user', ?, 'done', 'server', NULL, ?)", (chat_id + "-q", chat_id, f"{word} question", T0))
    state.DB.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                     " VALUES (?, ?, 'assistant', ?, 'done', 'server', 'm', ?)", (chat_id + "-a", chat_id, f"{word} answer", T0))
    doc = save(uid, b"%PDF " + word.encode(), "application/pdf", "doc.pdf")
    state.DB.execute("INSERT INTO attachments(id, user_id, message_id, kind, name, media_id, text, chars, created)"
                     " VALUES (?, ?, ?, 'document', 'doc.pdf', ?, ?, 5, ?)",
                     (chat_id + "-att", uid, chat_id + "-q", doc, f"{word} document", T0))
    return doc


@dataclass
class Made:
    """Запись вида корзины: id, её файлы, как удалить через API раздела и как увидеть в его ленте и по id."""
    id: str
    files: list
    delete: Callable
    visible: Callable                     # лента и запись по id — обе видят
    word: "str | None" = None             # слово поиска (запись в индексе)
    touch: "Callable | None" = None       # правка записи (триггер `*_au` поиска), пока она в корзине
    jobs: "Callable | None" = None        # запись видна в «В работе»
    extra: dict = field(default_factory=dict)


def make_chat(s, uid: int) -> Made:
    doc = new_chat(uid, "kiwi", "kiwi")
    chat.ANSWERED["kiwi-a"] = (uid, "kiwi", s.clock[0], "done")     # ответ через сервер кончился только что

    def visible():
        listed = "kiwi" in {c["id"] for c in s.c.get("/api/chats").json()["items"]}
        one = s.c.get("/api/chats/kiwi").status_code
        assert listed == (one == 200), (listed, one)
        return listed

    def touch():
        state.DB.execute("UPDATE messages SET content = 'kiwi answer edited' WHERE id = 'kiwi-a'")
        state.DB.execute("UPDATE chats SET title = 'kiwi title edited' WHERE id = 'kiwi'")
        state.DB.execute("UPDATE messages SET content = 'kiwi answer' WHERE id = 'kiwi-a'")
        state.DB.execute("UPDATE chats SET title = 'kiwi title' WHERE id = 'kiwi'")

    def jobs():
        return any(j["href"] == "chat/kiwi" for j in s.c.get("/api/jobs").json()["finished"])

    return Made("kiwi", [doc], lambda: s.c.delete("/api/chats/kiwi"), visible, word="kiwi", touch=touch, jobs=jobs)


def make_chat_media(s, uid: int) -> Made:
    new_chat(uid, "host", "host")
    clip = save(uid, b"dialog audio", "audio/mpeg", "voice.mp3")
    state.DB.execute("INSERT INTO chat_media(id, chat_id, user_id, name, mime, size, media_id, created)"
                     " VALUES ('dm1', 'host', ?, 'voice.mp3', 'audio/mpeg', 12, ?, ?)", (uid, clip, T0))

    def visible():
        return "dm1" in {m["id"] for m in s.c.get("/api/chats/host").json()["media"]}

    return Made("dm1", [clip], lambda: s.c.delete("/api/chats/host/media/dm1"), visible)


def section_made(s, item: str, files: list, url: str, *, word=None, touch_sql=None, href=None, listing=None) -> Made:
    """Запись раздела с лентой `url` и записью `url/<id>`: удаление — `DELETE url/<id>`; `touch_sql` — правка
    столбца поиска и обратно (`?` — id); `href` — ссылка строки «В работе»."""
    def visible():
        listed = item in {i["id"] for i in s.c.get(listing or url).json()["items"]}
        one = s.c.get(f"{url}/{item}").status_code
        assert listed == (one == 200), (listed, one)
        return listed

    def touch():
        for sql in touch_sql:
            state.DB.execute(sql, (item,))

    def jobs():
        return any(j["href"] == href for j in s.c.get("/api/jobs").json()["finished"])

    return Made(item, files, lambda: s.c.delete(f"{url}/{item}"), visible, word=word,
                touch=touch if touch_sql else None, jobs=jobs if href else None)


def make_image(s, uid: int) -> Made:
    source, picture = save(uid, b"source png", "image/png", "in.png"), save(uid, b"edited png", "image/png", "out.png")
    state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, source_media_id, media_id, state,"
                     " detail, created, finished) VALUES ('img1', ?, 'edit', 'flux', 'mango sunset', '1024x1024', 1, ?, ?,"
                     " 'done', 'готово', ?, ?)", (uid, source, picture, T0, s.clock[0]))
    return section_made(s, "img1", [source, picture], "/api/images", word="mango", href="photo?id=img1")


def make_video(s, uid: int) -> Made:
    source, clip = save(uid, b"source png", "image/png", "in.png"), save(uid, b"mp4 clip", "video/mp4", "clip.mp4")
    state.DB.execute("INSERT INTO videos(id, user_id, connection_id, gateway_id, source_media_id, media_id, prompt,"
                     " seconds, quality, smooth, state, progress, detail, created, finished) VALUES ('vid1', ?, 1,"
                     " 'video_1', ?, ?, 'papaya waves', 5, 'fast', 0, 'done', 100, 'готово', ?, ?)",
                     (uid, source, clip, T0, s.clock[0]))
    return section_made(s, "vid1", [source, clip], "/api/videos", word="papaya", href="video?id=vid1",
                        touch_sql=["UPDATE videos SET prompt = 'papaya dawn' WHERE id = ?",
                                   "UPDATE videos SET prompt = 'papaya waves' WHERE id = ?"])


SPEECH = ("INSERT INTO speeches(id, user_id, model, voice, voice_name, text, media_id, state, detail, created,"
          " finished, chat_id, chat_created) VALUES (?, ?, 'qwen3-tts', 'v', 'Voice', ?, ?, 'done', 'готово', ?, ?, ?, ?)")


def make_speech(s, uid: int) -> Made:
    clip = save(uid, b"speech mp3", "audio/mpeg", "speech.mp3")
    state.DB.execute(SPEECH, ("sp1", uid, "guava story", clip, T0, s.clock[0], "sp1", T0))
    return section_made(s, "sp1", [clip], "/api/speeches", word="guava", href="speech?id=sp1",
                        touch_sql=["UPDATE speeches SET text = 'guava tale' WHERE id = ?",
                                   "UPDATE speeches SET text = 'guava story' WHERE id = ?"])


def make_transcript(s, uid: int) -> Made:
    source = save(uid, b"m4a recording", "audio/mp4", "call.m4a")
    result = save(uid, json.dumps({"segments": [{"start": 0, "end": 1, "text": "lychee talk", "speaker": "SPEAKER_00"}]})
                  .encode(), "application/json", "result.json")
    state.DB.execute("INSERT INTO transcripts(id, user_id, model, name, source_media_id, result_media_id, state, detail,"
                     " created, finished, text) VALUES ('tr1', ?, 'whisperx', 'lychee.m4a', ?, ?, 'done', 'готово', ?, ?,"
                     " 'lychee talk')", (uid, source, result, T0, s.clock[0]))
    return section_made(s, "tr1", [source, result], "/api/transcripts", word="lychee", href="transcribe?id=tr1",
                        touch_sql=["UPDATE transcripts SET name = 'lychee 2.m4a' WHERE id = ?",
                                   "UPDATE transcripts SET name = 'lychee.m4a' WHERE id = ?"])


def make_scan(s, uid: int) -> Made:
    pdf = save(uid, b"%PDF- scan", "application/pdf", "deal.pdf")
    page = save(uid, b"page png", "image/png", "p1.png")
    state.DB.execute("INSERT INTO scans(id, user_id, model, name, source_media_id, origin, pages, done_pages, state,"
                     " detail, created, finished, text) VALUES ('sc1', ?, 'hunyuan-ocr', 'quince.pdf', ?, 'section', 1, 1,"
                     " 'done', 'готово', ?, ?, 'quince text')", (uid, pdf, T0, s.clock[0]))
    state.DB.execute("INSERT INTO scan_pages(scan_id, user_id, n, media_id, state, text) VALUES ('sc1', ?, 1, ?, 'done',"
                     " 'quince text')", (uid, page))
    return section_made(s, "sc1", [pdf, page], "/api/ocr", word="quince", href="ocr?id=sc1",
                        touch_sql=["UPDATE scans SET name = 'quince 2.pdf' WHERE id = ?",
                                   "UPDATE scans SET name = 'quince.pdf' WHERE id = ?"])


TRANSLATION = ("INSERT INTO translations(id, user_id, kind, model, source_lang, target_lang, name, source_media_id,"
               " scan_id, source, text, parts, done_parts, state, detail, created, finished) VALUES (?, ?, 'file',"
               " 'local-gemma-abl', 'en', 'ru', ?, ?, ?, ?, ?, 0, 0, 'done', 'готово', ?, ?)")


def make_translation(s, uid: int) -> Made:
    doc = save(uid, b"docx source", "application/octet-stream", "deal.docx")
    state.DB.execute(TRANSLATION, ("tl1", uid, "durian.docx", doc, None, "durian source", "durian перевод", T0,
                                   s.clock[0]))
    return section_made(s, "tl1", [doc], "/api/translate", word="durian", href="translate?id=tl1",
                        touch_sql=["UPDATE translations SET name = 'durian 2.docx' WHERE id = ?",
                                   "UPDATE translations SET name = 'durian.docx' WHERE id = ?"])


def make_voice(s, uid: int) -> Made:
    first, second = save(uid, b"wav one", "audio/wav", "v1.wav"), save(uid, b"wav two", "audio/wav", "v2.wav")
    # первый образец добавляет триггер `voices_sample_ai`
    state.DB.execute("INSERT INTO voices(id, user_id, name, ref_text, media_id, duration, created)"
                     " VALUES ('vo1', ?, 'Fig voice', 'fig text', ?, 6, ?)", (uid, first, T0))
    state.DB.execute("INSERT INTO voice_samples VALUES ('vs2', 'vo1', ?, 'fig two', ?, 7, ?)", (uid, second, T0))
    return section_made(s, "vo1", [first, second], "/api/voices")


#: вид корзины → запись этого вида у учётки `uid` (раздел добавляет свою строку вместе со строкой trash.KINDS)
MAKE = {"chats": make_chat, "chat_media": make_chat_media, "images": make_image, "videos": make_video, "speeches": make_speech, "transcripts": make_transcript, "scans": make_scan, "translations": make_translation, "voices": make_voice}


def owner(s) -> int:
    uid = make_user(s, "owner")
    signin(s, "owner")
    return uid


def hits(s, word: str) -> list:
    """id записей выдачи поиска (`POST /api/search`)."""
    return [i["id"] for i in s.c.post("/api/search", json={"q": word}).json()["items"]]


@pytest.fixture
def s(studio):
    return studio


def index_rows(item: str) -> list:
    return sorted(tuple(r) for r in state.DB.execute(
        "SELECT section, item, part, user_id, model, created, body FROM search_docs WHERE item = ?", (item,)))


@pytest.fixture(params=sorted(trash.KINDS))
def kind(request):
    assert request.param in MAKE, f"вид корзины {request.param} без строки MAKE"
    return request.param


def test_delete_hides_everywhere_and_restore_returns(s, kind):
    uid = owner(s)
    made = MAKE[kind](s, uid)
    assert made.visible()
    indexed = index_rows(made.id)
    if made.word:
        assert made.id in hits(s, made.word)
    if made.jobs:
        assert made.jobs()
    assert made.delete().status_code == 204
    assert not made.visible()
    assert made.delete().status_code == 404                      # запись в корзине по id — 404
    listed = [i for i in s.c.get("/api/trash").json() if i["kind"] == kind and i["id"] == made.id]
    sizes = sum(state.DB.execute("SELECT size FROM media WHERE id = ?", (m,)).fetchone()[0] for m in made.files)
    assert len(listed) == 1 and listed[0]["deleted"] == s.clock[0] and listed[0]["bytes"] == sizes
    assert listed[0]["section"] == trash.KINDS[kind].section and listed[0]["title"]
    for media_id in made.files:                                  # файлы — до окончательного удаления
        assert state.DB.execute("SELECT 1 FROM media WHERE id = ?", (media_id,)).fetchone()
    if made.word:
        assert made.id not in hits(s, made.word)
        if made.touch:                                           # правка в корзине снова вставляет строки индекса
            made.touch()
            assert index_rows(made.id) and made.id not in hits(s, made.word)
    if made.jobs:
        assert not made.jobs()
    assert s.c.post(f"/api/trash/{kind}/{made.id}/restore").status_code == 204
    assert made.visible()
    assert index_rows(made.id) == indexed
    if made.word:
        assert made.id in hits(s, made.word)
    if made.jobs:
        assert made.jobs()
    assert not [i for i in s.c.get("/api/trash").json() if i["id"] == made.id]
    assert s.c.post(f"/api/trash/{kind}/{made.id}/restore").status_code == 404
    assert s.c.post(f"/api/trash/nothing/{made.id}/restore").status_code == 404


def test_purge_after_14_days_removes_files_once(s, kind, unlinks):
    uid = owner(s)
    made = MAKE[kind](s, uid)
    table = trash.KINDS[kind].table
    paths = [state.CFG.media_dir / state.DB.execute("SELECT rel_path FROM media WHERE id = ?", (m,)).fetchone()[0]
             for m in made.files]
    assert made.delete().status_code == 204
    s.clock[0] += trash.TRASH_SEC - 1
    assert s.c.portal.call(trash.purge_expired) == 0
    assert state.DB.execute(f"SELECT 1 FROM {table} WHERE id = ?", (made.id,)).fetchone()
    s.clock[0] += 2
    assert trash.purge_due_now() is False                        # отметка прохода — только что
    assert s.c.portal.call(trash.purge_expired) == 1
    assert state.DB.execute(f"SELECT 1 FROM {table} WHERE id = ?", (made.id,)).fetchone() is None
    for media_id, path in zip(made.files, paths):
        assert state.DB.execute("SELECT 1 FROM media WHERE id = ?", (media_id,)).fetchone() is None
        assert not path.exists()
    assert index_rows(made.id) == []
    removed = len(unlinks)
    assert removed and all(not on_loop for on_loop in unlinks)
    assert s.c.portal.call(trash.purge_expired) == 0             # повтор — ни ошибок, ни новых удалений
    assert len(unlinks) == removed
    s.clock[0] += trash.PURGE_EVERY_SEC
    assert trash.purge_due_now() is True
    assert json.loads(state.DB.execute("SELECT value FROM settings WHERE key = ?",
                                       (trash.PURGE_KEY,)).fetchone()[0])["purged"] == 0


def test_delete_forever_takes_one_record_now(s, kind):
    uid = owner(s)
    made = MAKE[kind](s, uid)
    assert s.c.delete(f"/api/trash/{kind}/{made.id}").status_code == 404      # не в корзине — нельзя
    assert made.delete().status_code == 204
    assert s.c.delete(f"/api/trash/{kind}/{made.id}").status_code == 204
    assert state.DB.execute(f"SELECT 1 FROM {trash.KINDS[kind].table} WHERE id = ?", (made.id,)).fetchone() is None
    assert s.c.post(f"/api/trash/{kind}/{made.id}/restore").status_code == 404


def test_share_link_of_trashed_chat_is_unavailable_and_returns_after_restore(s):
    endpoint, _ = regular_chat(s)
    _, token = create_share(s, endpoint)
    opened = unlock(s, token)
    assert opened.status_code == 200
    access = opened.json()["access"]
    assert s.c.delete("/api/chats/chat").status_code == 204
    gone = unlock(s, token)
    assert gone.status_code == 404 and gone.json()["detail"]["code"] == "share_gone"
    assert s.c.post("/share/check", json={"access": access}).status_code == 404
    assert s.c.get(endpoint).status_code == 404                  # владелец ссылку чата из корзины не видит
    # «Доступы и ссылки»: ссылка остаётся (её можно отключить), но помечена — чат в корзине, ссылка недоступна
    assert [x["trashed"] for x in s.c.get("/api/me/access").json()["shares"]] == [True]
    assert s.c.post("/api/trash/chats/chat/restore").status_code == 204
    assert [x["trashed"] for x in s.c.get("/api/me/access").json()["shares"]] == [False]
    again = unlock(s, token)
    assert again.status_code == 200 and again.json()["messages"][1]["text"] == "**Private answer**"
    assert s.c.post("/share/check", json={"access": access}).status_code == 204


def test_speech_chat_delete_trashes_all_parts(s):
    """Удаление чата озвучки — `deleted` всем его записям: чат и записи уходят из лент, файлы — до окончательного
    удаления; «Вернуть» одной записи возвращает чат с ней одной."""
    uid = owner(s)
    clips = [save(uid, f"part {n}".encode(), "audio/mpeg", f"p{n}.mp3") for n in range(3)]
    for n, clip in enumerate(clips):
        state.DB.execute(SPEECH, (f"part{n}", uid, f"melon {n}", clip, T0 + n, T0 + n, "part0", T0))
    other = save(uid, b"other", "audio/mpeg", "o.mp3")
    state.DB.execute(SPEECH, ("solo", uid, "melon solo", other, T0 + 9, T0 + 9, "solo", T0 + 9))
    assert s.c.get("/api/speeches/chats/part0").status_code == 200
    assert s.c.delete("/api/speeches/chats/part0").status_code == 204
    assert [r[0] for r in state.DB.execute("SELECT id FROM speeches WHERE deleted = ? ORDER BY id",
                                           (s.clock[0],))] == ["part0", "part1", "part2"]
    assert s.c.get("/api/speeches/chats/part0").status_code == 404
    assert s.c.delete("/api/speeches/chats/part0").status_code == 404
    assert [c["id"] for c in s.c.get("/api/speeches/chats").json()["items"]] == ["solo"]
    assert [i["id"] for i in s.c.get("/api/speeches").json()["items"]] == ["solo"]
    assert hits(s, "melon") == ["solo"]
    assert sorted(i["id"] for i in s.c.get("/api/trash").json()) == ["part0", "part1", "part2"]
    assert all(state.DB.execute("SELECT 1 FROM media WHERE id = ?", (c,)).fetchone() for c in clips)
    assert s.c.post("/api/trash/speeches/part1/restore").status_code == 204
    assert [i["id"] for i in s.c.get("/api/speeches/chats/part0").json()["items"]] == ["part1"]
    s.clock[0] += trash.TRASH_SEC + 1
    assert s.c.portal.call(trash.purge_expired) == 2
    assert [r[0] for r in state.DB.execute("SELECT id FROM speeches ORDER BY id")] == ["part1", "solo"]
    assert [state.DB.execute("SELECT 1 FROM media WHERE id = ?", (c,)).fetchone() is not None
            for c in clips] == [False, True, False]


def test_translation_delete_keeps_scan_until_purge(s):
    """Удаление перевода больше не трогает его скан: документ «Сканов» и файлы остаются, пока запись в корзине;
    окончательное удаление уносит скан, если на него не ссылается другой перевод (и в корзине тоже)."""
    uid = owner(s)
    pdf = save(uid, b"%PDF- deal", "application/pdf", "deal.pdf")
    page = save(uid, b"page png", "image/png", "p1.png")
    state.DB.execute("INSERT INTO scans(id, user_id, model, name, source_media_id, origin, pages, done_pages, state,"
                     " detail, created, finished, text) VALUES ('scan', ?, 'hunyuan-ocr', 'deal.pdf', ?, 'section', 1, 1,"
                     " 'done', 'готово', ?, ?, 'pear text')", (uid, pdf, T0, T0))
    state.DB.execute("INSERT INTO scan_pages(scan_id, user_id, n, media_id, state, text) VALUES ('scan', ?, 1, ?,"
                     " 'done', 'pear text')", (uid, page))
    for tid in ("first", "again"):                               # повтор берёт тот же скан
        state.DB.execute(TRANSLATION, (tid, uid, "deal.pdf", pdf, "scan", "pear text", "груша", T0, T0))
    for tid in ("first", "again"):
        assert s.c.delete(f"/api/translate/{tid}").status_code == 204
    assert s.c.get("/api/ocr/scan").status_code == 200           # скан на месте и виден в «Сканах»
    assert "scan" in {i["id"] for i in s.c.get("/api/ocr").json()["items"]}
    assert s.c.delete("/api/trash/translations/first").status_code == 204
    assert s.c.get("/api/ocr/scan").status_code == 200           # «again» в корзине ещё ссылается на скан
    assert state.DB.execute("SELECT 1 FROM media WHERE id = ?", (pdf,)).fetchone()
    s.clock[0] += trash.TRASH_SEC + 1
    assert s.c.portal.call(trash.purge_expired) == 1
    assert state.DB.execute("SELECT count(*) FROM translations").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM scans").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media WHERE id IN (?, ?)", (pdf, page)).fetchone()[0] == 0


def test_takeout_skips_trash(s):
    uid = owner(s)
    kept_doc = new_chat(uid, "kept", "kept")
    gone_doc = new_chat(uid, "gone", "gone")
    clip = save(uid, b"dialog audio", "audio/mpeg", "voice.mp3")
    state.DB.execute("INSERT INTO chat_media(id, chat_id, user_id, name, mime, size, media_id, created)"
                     " VALUES ('dm1', 'kept', ?, 'voice.mp3', 'audio/mpeg', 12, ?, ?)", (uid, clip, T0))
    picture = save(uid, b"png", "image/png", "img.png")
    state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, media_id, state, detail, created,"
                     " finished, deleted) VALUES ('img', ?, 'generation', 'flux', 'p', '1024x1024', 1, ?, 'done', '',"
                     " ?, ?, ?)", (uid, picture, T0, T0, T0))
    assert s.c.delete("/api/chats/gone").status_code == 204
    assert s.c.delete("/api/chats/kept/media/dm1").status_code == 204
    r = s.c.post("/api/me/export", json={"sections": ["chat", "photo"]})
    assert r.status_code == 200 and r.json()["files"] == 1
    z = zipfile.ZipFile(io.BytesIO(s.c.get(f"/api/me/export/{r.json()['id']}").content))
    names = set(z.namelist())
    assert "chats/kept.json" in names and "chats/gone.json" not in names
    assert not any(gone_doc in n or clip in n or picture in n for n in names)
    assert any(kept_doc in n for n in names)
    assert json.loads(z.read("chats/kept.json"))["media"] == []
    assert json.loads(z.read("images/0001.json")) == []
    assert json.loads(z.read("manifest.json"))["counts"]["chats"] == 1


def test_admin_sees_no_foreign_trash(s):
    other = make_user(s, "other")
    new_chat(other, "theirs", "theirs")
    state.DB.execute("UPDATE chats SET deleted = ? WHERE id = 'theirs'", (T0,))
    make_user(s, "admin", is_admin=True)
    signin(s, "admin")
    assert s.c.get("/api/trash").json() == []
    assert s.c.post("/api/trash/chats/theirs/restore").status_code == 404
    assert s.c.delete("/api/trash/chats/theirs").status_code == 404
    assert state.DB.execute("SELECT deleted FROM chats WHERE id = 'theirs'").fetchone()[0] == T0


def test_trash_list_hides_locked_sections_and_actions_wait_for_unlock(s):
    uid = owner(s)
    new_chat(uid, "c1", "plum")
    assert s.c.delete("/api/chats/c1").status_code == 204
    state.DB.execute("UPDATE users SET screen_lock = 1, screen_lock_sections = '[\"chat\"]' WHERE id = ?", (uid,))
    state.DB.execute("UPDATE sessions SET seen = NULL WHERE user_id = ?", (uid,))
    assert s.c.get("/api/trash").json() == []
    assert s.c.post("/api/trash/chats/c1/restore").status_code == 423
    assert s.c.delete("/api/trash/chats/c1").status_code == 423


def test_history_erase_and_empty_trash_need_password_and_count_failures(s):
    """«Удалить всю историю» и «Очистить корзину» — окончательно и только паролем учётки: та же проверка и паузы,
    что у выключения блокировки и смены пароля (`auth.confirm_password`), неудачи всех этих действий — в один счёт."""
    uid = owner(s)
    kept_doc = new_chat(uid, "kept", "kept")
    gone_doc = new_chat(uid, "gone", "gone")
    assert s.c.delete("/api/chats/gone").status_code == 204
    erase = lambda password: s.c.request("DELETE", "/api/history", json={"password": password})     # noqa: E731
    empty = lambda password: s.c.post("/api/trash/empty", json={"password": password})            # noqa: E731
    assert s.c.delete("/api/history").status_code == 422              # без тела — не стирает
    assert erase("").status_code == 422 and empty("").status_code == 422      # пустое поле — до счёта
    assert erase("wrong-password").status_code == 403
    assert empty("wrong-password").status_code == 403
    assert state.DB.execute("SELECT count(*) FROM chats").fetchone()[0] == 2
    for _ in range(auth.LOCK_FAILS - 2):
        assert s.c.post("/api/me/password", json={"current": "wrong", "new": "new-password-123"}).status_code == 403
    assert empty("password-123").status_code == 429                            # пауза — общая с прочими действиями
    assert erase("password-123").status_code == 429
    s.clock[0] += auth.LOCK_SEC
    r = empty("password-123")
    assert r.status_code == 200 and r.json() == {"purged": 1}
    assert [r[0] for r in state.DB.execute("SELECT id FROM chats")] == ["kept"]
    assert state.DB.execute("SELECT 1 FROM media WHERE id = ?", (gone_doc,)).fetchone() is None
    assert s.c.get("/api/trash").json() == []
    assert s.c.delete("/api/chats/kept").status_code == 204                   # в корзине — и её стирает история
    r = erase("password-123")
    assert r.status_code == 200 and r.headers["clear-site-data"] == auth.CLEAR_CACHE
    assert state.DB.execute("SELECT count(*) FROM chats").fetchone()[0] == 0
    assert state.DB.execute("SELECT 1 FROM media WHERE id = ?", (kept_doc,)).fetchone() is None


def test_storage_heaviest_and_failed_move_to_trash(s):
    """«Хранилище»: самые тяжёлые живые записи (по сумме `media.size` их файлов), корзина и неудачные по разделам;
    `POST /api/me/storage/failed` переносит неудачные и отменённые записи разделов в корзину."""
    uid = owner(s)
    make_image(s, uid)                                                         # 20 байт: in.png + out.png
    new_chat(uid, "kiwi", "kiwi")                                              # 9 байт: doc.pdf
    big = save(uid, b"x" * 50, "video/mp4", "clip.mp4")
    state.DB.execute("INSERT INTO videos(id, user_id, connection_id, gateway_id, source_media_id, media_id, prompt,"
                     " seconds, quality, smooth, state, progress, detail, created, finished) VALUES ('bad', ?, 1, 'v',"
                     " ?, NULL, 'broken waves', 5, 'fast', 0, 'failed', 0, 'сбой', ?, ?)", (uid, big, T0, T0))
    for tid, st in (("tr-err", "error"), ("tr-off", "cancelled"), ("tr-ok", "done")):
        state.DB.execute("INSERT INTO transcripts(id, user_id, model, name, source_media_id, state, detail, created)"
                         " VALUES (?, ?, 'whisperx', 'call.m4a', 'no-file', ?, '', ?)", (tid, uid, st, T0))
    new_chat(uid, "gone", "gone")
    assert s.c.delete("/api/chats/gone").status_code == 204
    other = make_user(s, "other")
    new_chat(other, "theirs", "theirs")                                        # чужое не в счёт
    body = s.c.get("/api/me/storage").json()
    assert body["heaviest"][:3] == [{"section": "video", "id": "bad", "title": "broken waves", "bytes": 50},
                                    {"section": "photo", "id": "img1", "title": "mango sunset", "bytes": 20},
                                    {"section": "chat", "id": "kiwi", "title": "kiwi title", "bytes": 9}]
    assert "gone" not in {h["id"] for h in body["heaviest"]}
    assert body["trash"] == {"chat": {"count": 1, "bytes": 9}}
    assert body["failed"] == {"video": 1, "transcribe": 2}
    assert body["total"]["files"] == 5                                         # прежние поля — на месте
    assert s.c.post("/api/me/storage/failed", json={"sections": ["nothing"]}).status_code == 422
    r = s.c.post("/api/me/storage/failed", json={"sections": ["transcribe"]})
    assert r.status_code == 200 and r.json() == {"moved": 2}
    assert sorted(i["id"] for i in s.c.get("/api/trash").json()) == ["gone", "tr-err", "tr-off"]
    after = s.c.get("/api/me/storage").json()
    assert after["failed"] == {"video": 1}
    assert after["trash"] == {"chat": {"count": 1, "bytes": 9}, "transcribe": {"count": 2, "bytes": 0}}
    state.DB.execute("UPDATE users SET screen_lock = 1, screen_lock_sections = '[\"video\"]' WHERE id = ?", (uid,))
    state.DB.execute("UPDATE sessions SET seen = NULL WHERE user_id = ?", (uid,))
    assert "video" not in {h["section"] for h in s.c.get("/api/me/storage").json()["heaviest"]}
    assert s.c.post("/api/me/storage/failed", json={"sections": ["video"]}).status_code == 423


@pytest.mark.parametrize("section", sorted(search.TITLES))
def test_reindex_item_writes_the_rows_of_the_triggers(s, section):
    """`search.reindex_item` — те же строки индекса, что вставили триггеры (тело, модель, время, части чата)."""
    uid = make_user(s, "owner")
    if section == "chat":
        new_chat(uid, "rec", "ёлка")
    else:
        from test_search import IMAGE, SCAN, SPEECH, TRANSCRIPT, TRANSLATION, VIDEO
        sql, args = {"photo": (IMAGE, ("rec", uid, "flux", "Ёлка", 1)), "video": (VIDEO, ("rec", uid, "Ёлка", 2)),
                     "transcribe": (TRANSCRIPT, ("rec", uid, "Ёлка.m4a", 3, "ёлка речь")),
                     "speech": (SPEECH, ("rec", uid, "Ёлка", 4)), "ocr": (SCAN, ("rec", uid, "Ёлка.pdf", 5, "ёлка текст")),
                     "translate": (TRANSLATION, ("rec", uid, "Ёлка", "ёлка исходник", 6))}[section]
        state.DB.execute(sql, args)
        if section == "translate":
            state.DB.execute("UPDATE translations SET text = 'ёлка перевод' WHERE id = 'rec'")
    before = index_rows("rec")
    assert before
    table = search.TITLES[section][0]
    state.DB.execute(f"UPDATE {table} SET deleted = 1 WHERE id = 'rec'")
    assert index_rows("rec") == []
    search.reindex_item(section, "rec")                          # в корзине — не вставляет
    assert index_rows("rec") == []
    state.DB.execute(f"UPDATE {table} SET deleted = NULL WHERE id = 'rec'")
    search.reindex_item(section, "rec")
    search.reindex_item(section, "rec")                          # повтор — без дублей
    assert index_rows("rec") == before
    assert {r[0] for r in state.DB.execute("SELECT d.item FROM search JOIN search_docs d ON d.id = search.rowid"
                                           " WHERE search MATCH 'елка*'")} == {"rec"}
