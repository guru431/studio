"""Вся история пользователя: чаты с сообщениями, вложения, картинки, ролики и файлы media."""
import asyncio
import logging

import chat
import images
import media
import state
import videos

log = logging.getLogger("studio")


async def delete_all(user_id: int) -> None:
    """Идущие задания и ответы останавливаются, ролики снимаются у шлюза, затем файлы (все — одним
    вызовом в потоке), затем строки. Файл, который не удалился, удаление строк не останавливает:
    иначе история застряла бы в базе из-за одного битого файла."""
    images.forget_user(user_id)                   # картинку, пришедшую после, задание выбросит само
    await chat.stop_user(user_id)
    await videos.cancel_at_gateway(user_id)
    rows = [(r["id"], r["rel_path"]) for r in
            state.DB.execute("SELECT id, rel_path FROM media WHERE user_id = ?", (user_id,))]
    failed = await asyncio.to_thread(media.remove_files, rows)
    if failed:
        log.warning("история пользователя %d: не удалилось файлов media — %d", user_id, failed)
    state.DB.execute("BEGIN")
    try:
        state.DB.execute("DELETE FROM attachments WHERE user_id = ?", (user_id,))
        state.DB.execute("DELETE FROM chats WHERE user_id = ?", (user_id,))      # сообщения — каскадом
        for table in ("images", "videos", "media"):
            state.DB.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    log.info("история пользователя %d удалена", user_id)
