"""Projects preserve existing chats and files and isolate context at the model boundary."""
import io
import zipfile
from contextlib import closing
from urllib.parse import quote

import db
import state
from conftest import T0, add_gateway, erase_history, make_user, signin
from historical_schema import install_schema
from test_chat import attach, new_chat, png, sse, turn
from test_chat_pending import answer, pending_of
from test_chat_web import call
from test_takeout import save

BASE = "/api/chat-projects"


def project(studio, name, **data):
    response = studio.c.post(BASE, json={"name": name} | data)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_step_84_backfills_existing_and_future_users(tmp_path):
    with closing(db.connect(tmp_path / "studio.db")) as conn:
        install_schema(conn, 83)
        conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'a', 'A', 'hash', ?)", (T0,))
        conn.execute("INSERT INTO chats(id, user_id, created, updated) VALUES ('old', 1, ?, ?)", (T0, T0))
        db.migrate(conn)
        db.migrate(conn)
        conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (2, 'b', 'B', 'hash', ?)", (T0,))
        conn.execute("INSERT INTO chats(id, user_id, created, updated) VALUES ('new', 2, ?, ?)", (T0, T0))
        assert [tuple(r) for r in conn.execute("SELECT id, name, inbox FROM chat_projects ORDER BY id")] == [
            ("inbox-1", "Без проекта", 1), ("inbox-2", "Без проекта", 1)]
        assert [tuple(r) for r in conn.execute("SELECT id, project_id FROM chats ORDER BY user_id")] == [
            ("old", "inbox-1"), ("new", "inbox-2")]
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_project_instructions_and_confirmed_memory_are_used_only_in_its_chats(studio):
    add_gateway(studio)
    owner = studio.c.get("/api/me").json()["id"]
    inbox = studio.c.get(BASE).json()["items"][0]
    assert inbox["name"] == "Без проекта" and inbox["inbox"]
    first = project(studio, "Release", context="Deploy only after acceptance")
    other = project(studio, "Research", context="Cite primary sources")
    assert studio.c.get(BASE).json()["items"][0]["id"] == inbox["id"]
    chat = new_chat(studio, project_id=first)
    assert studio.c.get("/api/chats").json()["items"][0]["project_id"] == first
    studio.gw.chat_script = [{"tool_calls": [call("memory_save", text="Use the staging queue")]}, ["Saved"]]
    shown = pending_of(sse(turn(studio, chat, "Remember this for the release")))
    assert studio.c.patch(f"/api/chats/{chat}", json={"project_id": other}).status_code == 409
    assert sse(answer(studio, chat, shown["id"], "run"))[-1][0] == "done"
    assert studio.c.get("/api/memory").json()["items"] == []
    assert studio.c.get(f"{BASE}/{first}/memory").json()["items"][0]["text"] == "Use the staging queue"
    assert b"staging" not in state.DB.execute("SELECT text_enc FROM chat_project_memory").fetchone()[0]

    second = new_chat(studio, project_id=first)
    studio.gw.chat_script = [["OK"]]
    sse(turn(studio, second, "What are our instructions?"))
    system = studio.gw.chat_requests[-1]["messages"][0]["content"]
    assert "Deploy only after acceptance" in system and "Use the staging queue" in system

    assert studio.c.patch(f"/api/chats/{second}", json={"project_id": other}).json()["project_id"] == other
    studio.gw.chat_script = [["OK"]]
    sse(turn(studio, second, "What are our instructions now?"))
    system = studio.gw.chat_requests[-1]["messages"][0]["content"]
    assert "Cite primary sources" in system and "staging queue" not in system
    assert "Deploy only after acceptance" not in system

    make_user(studio, "other-project-owner")
    foreign = state.DB.execute("SELECT id FROM chat_projects WHERE inbox = 1 AND user_id != ?", (owner,)).fetchone()[0]
    assert studio.c.patch(f"/api/chats/{second}", json={"project_id": foreign}).status_code == 404
    signin(studio, "other-project-owner")
    assert studio.c.get(f"{BASE}/{first}/memory").status_code == 404


def test_project_grant_roles_protect_private_data_and_revoke_access(studio):
    owner_connection = add_gateway(studio)
    owner = studio.c.get("/api/me").json()["id"]
    member = make_user(studio, "project-member")
    state.DB.execute("UPDATE users SET email = ? WHERE id = ?", ("member@example.test", member))
    pid = project(studio, "Release", context="Private project instructions")
    base = f"{BASE}/{pid}"
    assert studio.c.post(f"{base}/memory", json={"text": "Private memory"}).status_code == 201
    chat_id = new_chat(studio, project_id=pid)
    private = new_chat(studio)
    assert studio.c.patch(f"/api/chats/{chat_id}", json={"title": "Release chat"}).status_code == 200
    studio.gw.chat_script = [["Visible answer"]]
    assert sse(turn(studio, chat_id, "Visible question"))[-1][0] == "done"
    file_row = studio.c.post(f"{base}/files/upload", headers={"x-studio-name": "readme.txt"}, content=b"notes").json()
    file_id = file_row["id"]
    assert studio.c.get(f"{base}/files/content/{file_id}").content == b"notes"
    assert studio.c.post(f"{BASE}/inbox-{owner}/members", json={"login": "project-member", "role": "viewer"}).status_code == 404
    assert studio.c.post(f"{base}/members", json={"login": "member@example.test", "role": "viewer"}).status_code == 201
    assert studio.c.get(f"{base}/members").json()["items"] == [
        {"id": member, "login": "project-member", "disabled": 0, "role": "viewer"}]

    signin(studio, "project-member")
    member_connection = add_gateway(studio)
    shared = next(item for item in studio.c.get(BASE).json()["items"] if item["id"] == pid)
    assert shared["role"] == "viewer" and "context" not in shared and "memory_on" not in shared
    assert studio.c.get(f"{base}/memory").status_code == 404
    assert studio.c.get(f"{base}/members").status_code == 404
    assert studio.c.put(base, json={"name": "No"}).status_code == 404
    assert studio.c.get("/api/chats").json()["items"] == [
        {"id": chat_id, "title": "Release chat", "updated": T0, "project_id": pid, "readonly": True}]
    assert studio.c.get(f"/api/chats/{chat_id}").status_code == 404
    assert studio.c.get(f"{base}/chats/{private}").status_code == 404
    assert studio.c.get(f"{base}/chats/{chat_id}").json() == {"title": "Release chat", "messages": [
        {"role": "user", "content": "Visible question"}, {"role": "assistant", "content": "Visible answer"}]}
    entry = studio.c.get(f"{base}/files").json()["entries"][0]
    downloaded = studio.c.get(entry["download"])
    assert downloaded.content == b"notes" and downloaded.headers["cache-control"] == "no-store"
    assert studio.c.get(entry["preview"]["url"]).status_code == 200
    assert studio.c.delete(f"{base}/files/{file_id}").status_code == 403
    assert studio.c.post(f"{base}/files/folders", json={"name": "No"}).status_code == 403
    assert studio.c.post("/api/chats", json={"project_id": pid}).status_code == 403

    signin(studio, "owner")
    assert studio.c.patch(f"{base}/members/{member}", json={"role": "editor"}).status_code == 204
    signin(studio, "project-member")
    editor_chat = studio.c.post("/api/chats", json={"project_id": pid, "connection_id": member_connection})
    assert editor_chat.status_code == 201, editor_chat.text
    assert editor_chat.json()["connection_id"] == member_connection != owner_connection
    own_chat = editor_chat.json()["id"]
    studio.gw.chat_script = [["Editor answer"]]
    assert sse(turn(studio, own_chat, "Editor question"))[-1][0] == "done"
    assert "Private" not in studio.gw.chat_requests[-1]["messages"][0]["content"]
    assert studio.c.patch(f"/api/chats/{chat_id}", json={"title": "No"}).status_code == 404
    assert studio.c.delete(f"{base}/files/{file_id}").status_code == 204
    contribution = studio.c.post(f"{base}/files/upload", headers={"x-studio-name": "editor.txt"}, content=b"edited").json()
    trashed_chat = new_chat(studio, project_id=pid)
    assert studio.c.delete(f"/api/chats/{trashed_chat}").status_code == 204

    signin(studio, "owner")
    assert studio.c.get(f"{base}/chats/{own_chat}").json()["messages"][-1]["content"] == "Editor answer"
    assert studio.c.patch(f"{base}/members/{member}", json={"role": "viewer"}).status_code == 204
    signin(studio, "project-member")
    assert studio.c.patch(f"/api/chats/{own_chat}", json={"title": "No"}).status_code == 403
    assert studio.c.post(f"/api/trash/chats/{trashed_chat}/restore").status_code == 403
    signin(studio, "owner")
    assert studio.c.delete(f"{base}/members/{member}").status_code == 204
    assert studio.c.get(f"{base}/files/content/{contribution['id']}").content == b"edited"
    signin(studio, "project-member")
    assert pid not in {item["id"] for item in studio.c.get(BASE).json()["items"]}
    assert studio.c.get(f"{base}/chats/{chat_id}").status_code == 404
    assert studio.c.get(f"{base}/files/content/{contribution['id']}").status_code == 404
    assert studio.c.get(f"/api/chats/{own_chat}").json()["chat"]["project_id"] == f"inbox-{member}"


def test_shared_project_hides_protected_chat_and_its_file_aliases(studio):
    add_gateway(studio)
    make_user(studio, "project-viewer")
    pid = project(studio, "Release")
    base = f"{BASE}/{pid}"
    files = f"{base}/files"
    chat_id = new_chat(studio, project_id=pid)
    attachment = attach(studio, "protected.png", png(), "image/png")
    studio.gw.chat_script = [["Hidden"]]
    assert sse(turn(studio, chat_id, "Hidden", attachment_ids=[attachment["id"]]))[-1][0] == "done"
    media_id, author = state.DB.execute("SELECT media_id, user_id FROM attachments WHERE id = ?",
                                        (attachment["id"],)).fetchone()
    state.DB.execute("UPDATE chats SET protected_used = 1 WHERE id = ?", (chat_id,))
    state.DB.execute("INSERT INTO chat_project_files(id, project_id, user_id, name, name_key, created, media_id)"
                     " VALUES ('alias', ?, ?, 'alias.png', 'alias.png', ?, ?)", (pid, author, T0, media_id))
    assert studio.c.post(f"{base}/members", json={"login": "project-viewer", "role": "viewer"}).status_code == 201
    signin(studio, "project-viewer")
    assert studio.c.get("/api/chats").json()["items"] == []
    assert studio.c.get(f"{base}/chats/{chat_id}").status_code == 404
    assert studio.c.get(files).json()["entries"] == []
    assert studio.c.get(f"{files}?parent=chats").json()["entries"] == []
    assert studio.c.get(f"{files}/content/chat-{media_id}").status_code == 404
    assert studio.c.get(f"{files}/content/alias").status_code == 404
    with zipfile.ZipFile(io.BytesIO(studio.c.get(f"{files}/zip").content)) as archive:
        assert not archive.namelist()


def test_project_files_follow_chats_and_zip_nested_storage(studio):
    add_gateway(studio)
    uid = studio.c.get("/api/me").json()["id"]
    inbox = studio.c.get(BASE).json()["items"][0]["id"]
    target = project(studio, "Delivery")
    files = f"{BASE}/{target}/files"
    chat = new_chat(studio)
    image = attach(studio, "old.png", png(), "image/png")
    studio.gw.chat_script = [["OK"]]
    sse(turn(studio, chat, attachment_ids=[image["id"]]))
    old_chat_files = f"{BASE}/{inbox}/files?parent=chats"
    assert [entry["name"] for entry in studio.c.get(old_chat_files).json()["entries"]] == ["old.png"]
    assert studio.c.patch(f"/api/chats/{chat}", json={"project_id": target}).status_code == 200
    assert studio.c.get(old_chat_files).json()["entries"] == []

    photo = save(uid, png(), "image/png", "photo.png")
    state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, media_id, state, detail, created)"
                     " VALUES (?, ?, 'generation', 'flux', 'Sample', '64x48', 1, ?, 'done', '', ?)", (photo, uid, photo, T0))
    output = studio.c.post("/api/my-files/0/list?section=photo", json={"path": ""}).json()["entries"]
    assert [entry["id"] for entry in output] == [photo]
    assert studio.c.post(f"{files}/from-section", json={"media_id": photo, "section": "photo"}).status_code == 201

    folder = studio.c.post(f"{files}/folders", json={"name": "Docs"}).json()["id"]
    child = studio.c.post(f"{files}/folders", json={"name": "Reports", "parent": folder}).json()["id"]
    headers = {"x-studio-folder": child, "x-studio-name": quote("отчёт.txt")}
    assert studio.c.post(f"{files}/upload", headers=headers, content=b"Report").status_code == 201
    assert studio.c.post(f"{files}/upload", headers=headers, content=b"Duplicate").status_code == 409
    assert studio.c.post(f"{files}/folders", json={"name": "..", "parent": child}).status_code == 422
    assert studio.c.delete(f"{files}/{folder}").status_code == 409
    with zipfile.ZipFile(io.BytesIO(studio.c.get(f"{files}/zip").content)) as archive:
        assert archive.testzip() is None
        assert archive.getinfo("Docs/Reports/").is_dir()
        assert archive.read("Docs/Reports/отчёт.txt") == b"Report"
        assert archive.read("photo.png") == png()
        assert archive.read("Файлы диалогов/old.png") == png()

    linked = next(entry for entry in studio.c.get(files).json()["entries"] if entry.get("name") == "photo.png")
    assert studio.c.delete(f"{files}/{linked['id']}").status_code == 204
    assert studio.c.get(f"/api/media/{photo}").status_code == 200
    state.DB.execute("UPDATE chats SET protected_used = 1 WHERE id = ?", (chat,))
    assert studio.c.get(f"{files}/zip").status_code == 409
    assert erase_history(studio).status_code in (200, 204)
    assert [item["id"] for item in studio.c.get(BASE).json()["items"]] == [inbox]
