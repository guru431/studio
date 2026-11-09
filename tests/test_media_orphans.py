"""Ссылки на файлы media и ежедневная чистка «сирот» (идея и находка 2026-11-09).

Ошибка в списке ссылок — потеря файлов пользователя: реестр сверяет каждый столбец схемы `*media_id`
с media.REFERENCES, а для каждого столбца запись с этим id держит файл и в referenced, и в чистке."""
import json
import sqlite3
import uuid

import pytest

import db
import media
import state
from conftest import make_user, signin

DAY = media.ORPHAN_AGE_SEC


def schema_columns() -> set[tuple[str, str]]:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    return {(t, c) for t, c in conn.execute(
        "SELECT m.name, p.name FROM sqlite_master m JOIN pragma_table_info(m.name) p WHERE m.type = 'table'")}


def test_every_media_id_column_of_the_schema_is_a_known_reference():
    columns = schema_columns()
    media_ids = {(t, c) for t, c in columns if c.endswith("media_id")}
    registered = {(t, c) for t, cols in media.REFERENCES.items() for c in cols}
    assert media_ids == registered | media.NOT_REFERENCES
    assert registered <= columns


def insert_referencing(table: str, column: str, uid: int, media_id: str) -> None:
    """Строка таблицы с id файла в столбце: NOT NULL без умолчания — заглушки по типу; CHECK и FK выключены
    (реестр о ссылке, а не о прочих правилах таблицы)."""
    cols = state.DB.execute(f"PRAGMA table_info({table})").fetchall()
    values = {}
    for c in cols:
        if c["name"] == column:
            values[c["name"]] = media_id
        elif c["name"] == "user_id":
            values[c["name"]] = uid
        elif (c["notnull"] or c["pk"]) and c["dflt_value"] is None:
            kind = c["type"].upper()
            values[c["name"]] = (uuid.uuid4().hex if "TEXT" in kind else b"x" if "BLOB" in kind
                                 else 0.0 if "REAL" in kind else 1)
    if table == "chat_share_files":
        share = values["share_id"] = uuid.uuid4().hex
        insert_referencing("chat_shares", "id", uid, share)
    names = ", ".join(values)
    state.DB.execute(f"INSERT INTO {table}({names}) VALUES ({', '.join('?' * len(values))})", list(values.values()))


@pytest.fixture
def loose(studio):
    state.DB.execute("PRAGMA foreign_keys = OFF")
    state.DB.execute("PRAGMA ignore_check_constraints = ON")
    yield studio
    state.DB.execute("PRAGMA ignore_check_constraints = OFF")
    state.DB.execute("PRAGMA foreign_keys = ON")


def old_file(studio, uid: int, name: str = "x.png") -> str:
    mid = media.save(uid, b"data", "image/png", name)
    state.DB.execute("UPDATE media SET created = ? WHERE id = ?", (studio.clock[0] - DAY - 1, mid))
    return mid


def sweep(studio):
    return studio.c.portal.call(media.sweep_orphans)


def alive(mid: str) -> bool:
    return state.DB.execute("SELECT 1 FROM media WHERE id = ?", (mid,)).fetchone() is not None


@pytest.mark.parametrize("table, column", sorted((t, c) for t, cols in media.REFERENCES.items() for c in cols))
def test_each_reference_keeps_its_file_and_its_removal_lets_the_sweep_take_it(loose, env, table, column):
    studio = loose
    uid = make_user(studio, "owner")
    bob = make_user(studio, "bob")
    mid = old_file(studio, uid)
    insert_referencing(table, column, uid, mid)
    assert media.referenced(uid, mid) and not media.referenced(bob, mid)
    assert sweep(studio) == 0 and alive(mid)
    for t in ("chat_share_files", "chat_shares", table):
        state.DB.execute(f"DELETE FROM {t}")
    assert not media.referenced(uid, mid)
    assert sweep(studio) == 1 and not alive(mid)
    assert not (env.media_dir / f"u{uid}").exists() or not list((env.media_dir / f"u{uid}").rglob("*.png"))


def test_a_file_whose_record_never_came_is_swept_after_a_day(studio, env):
    """Находка 2026-11-09: остановка службы между сохранением файла и записью истории (ролик, правка кистью)
    оставляла строку media без записи — её видело только «Хранилище»."""
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    fresh = media.save(uid, b"new", "image/png", "fresh.png")
    orphan = old_file(studio, uid, "orphan.png")
    kept = old_file(studio, uid, "kept.png")
    saving = old_file(studio, uid, "saving.png")
    # NULL в столбцах ссылок (готовящаяся картинка без итога) не должен отключать NOT IN
    state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, source_media_id, media_id,"
                     " state, detail, created) VALUES ('i1', ?, 'edit', 'flux', 'p', '1x1', 1, ?, NULL, 'working', '', 1)",
                     (uid, kept))
    media.SAVING.add(saving)
    try:
        assert sweep(studio) == 1
    finally:
        media.SAVING.discard(saving)
    assert not alive(orphan) and alive(fresh) and alive(kept) and alive(saving)
    assert studio.c.get(f"/api/media/{orphan}").status_code == 404
    assert media.waiting(uid)[0] == 0                       # файл удалён сразу, очередь пуста
    assert not any(p.name.startswith(orphan) for p in env.media_dir.rglob("*"))


def test_the_sweep_is_skipped_on_a_base_with_unknown_file_references(studio):
    """Откат кода после шага схемы с новой ссылкой на файлы: удалять по неполному списку — терять файлы."""
    uid = make_user(studio, "owner")
    orphan = old_file(studio, uid)
    state.DB.execute("CREATE TABLE future_things(id TEXT PRIMARY KEY, user_id INTEGER, cover_media_id TEXT)")
    assert sweep(studio) is None and alive(orphan)
    assert media.orphans()["found"] is None
    state.DB.execute("DROP TABLE future_things")
    version = state.DB.execute("PRAGMA user_version").fetchone()[0]
    state.DB.execute(f"PRAGMA user_version = {db.VERSION + 1}")
    try:
        assert sweep(studio) is None and alive(orphan)
    finally:
        state.DB.execute(f"PRAGMA user_version = {version}")
    assert sweep(studio) == 1 and not alive(orphan)


def test_the_sweep_runs_once_a_day_and_the_admin_sees_the_count(studio):
    uid = make_user(studio, "admin", is_admin=True)
    signin(studio, "admin")
    assert media.orphans_due() and studio.c.get("/api/admin/storage").json()["orphans"] is None
    old_file(studio, uid)
    old_file(studio, uid)
    assert sweep(studio) == 2
    assert not media.orphans_due()
    assert studio.c.get("/api/admin/storage").json()["orphans"] == {"at": studio.clock[0], "found": 2}
    studio.clock[0] += media.ORPHAN_EVERY_SEC
    assert media.orphans_due()
    assert json.loads(state.DB.execute("SELECT value FROM settings WHERE key = 'orphans'").fetchone()[0])["found"] == 2
