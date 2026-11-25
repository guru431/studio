"""«Новый диалог с итогом» (спека 2026-11-25-studio-skills-status-local-models п. 27): модель пересказывает весь
диалог механизмом «Сжать сейчас» (`chat.summarize_dialog`, п. 30) — допуски, согласие на расшифровку и отказы те же.
Наследник — `chat_fork.new_dialog` без сообщений: подключение, модель, роль, рассуждение, MCP, базы знаний,
название и отметка защищённого источника — как у исходного. Итог уходит только ответом — странице в поле ввода;
ни в `chats.summary` прежнего, ни в базу нового он не пишется. Диалог через Cleaner (предпросмотр или шлюз
`cleaner_route`) итогом не продолжается: контексты замен Cleaner привязаны к диалогу."""
import logging

from fastapi import APIRouter, HTTPException, Request

import chat
import chat_fork
import cleaner_client
import operations
import state

NOTHING = "в диалоге ещё нет ответа — итог писать не из чего"
CLEANER = "диалог через Cleaner не продолжается итогом: замены Cleaner привязаны к диалогу"
#: свои слова отказов механизма «Сжать сейчас» (`chat.summarize_dialog`): коды прежние, речь — об итоге, не о сжатии
BUSY = "идёт пересказ диалога — дождитесь"
NO_SUMMARY = "новый диалог с итогом — только для моделей через сервер, кроме Cleaner и MCP"
NO_LIMIT = "предел контекста модели неизвестен — итог не составить"

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")


def _reworded(exc: HTTPException) -> HTTPException:
    """Отказ `summarize_dialog` или `_busy` словами «Нового диалога с итогом» (`no_compact`, `no_limit`, `nothing`,
    `busy` пересказа); прочие — как есть."""
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    code = detail.get("code")
    words = {"no_compact": NO_SUMMARY, "no_limit": NO_LIMIT, "nothing": NOTHING}.get(code)
    if code == "busy" and detail.get("message") == chat.COMPACTING_BUSY:
        words = BUSY
    return exc if words is None else HTTPException(exc.status_code, {**detail, "message": words})


@router.post("/chats/{chat_id}/continue", status_code=201)
async def continue_chat(chat_id: str, request: Request):
    """Итог диалога и наследник: 201 `{chat_id, text}`. Нет диалога — 404 `gone`; нет выбранного ответа с текстом —
    409 `nothing`; диалог через Cleaner — 409 `cleaner`; прочие отказы — `chat.summarize_dialog`."""
    uid = request.state.user.id
    row = chat._chat_row(uid, chat_id)
    try:
        chat._busy(chat_id)
    except HTTPException as exc:
        raise _reworded(exc) from None
    if state.DB.execute("SELECT 1 FROM messages WHERE chat_id = ? AND role = 'assistant' AND unchosen = 0"
                        " AND status IN ('done', 'stopped') AND content <> '' LIMIT 1", (chat_id,)).fetchone() is None:
        raise HTTPException(409, {"code": "nothing", "message": NOTHING})
    if chat_fork._through_cleaner(uid, row["connection_id"]):
        raise cleaner_client.refused(CLEANER)
    last = state.DB.execute("SELECT id FROM messages WHERE chat_id = ? AND unchosen = 0"
                            " ORDER BY created DESC, rowid DESC LIMIT 1", (chat_id,)).fetchone()
    try:
        text = await chat.summarize_dialog(uid, row, last["id"], keep_skill=True)   # навык действует весь диалог
    except HTTPException as exc:
        raise _reworded(exc) from None
    operations.check(uid)
    fresh = chat._chat_row(uid, chat_id)    # удалён — 404; отметка защищённого источника могла встать за время итога
    state.DB.execute("BEGIN")
    try:
        new = chat_fork.new_dialog(uid, fresh)
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    log.info("чат %s: диалог с итогом %s, знаков %d", chat_id[:8], new[:8], len(text))
    return {"chat_id": new, "text": text}
