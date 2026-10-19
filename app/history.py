"""Вся история пользователя: чаты с сообщениями, вложения, картинки, ролики и файлы media."""
import logging

import media
import state
import videos

log = logging.getLogger("studio")


async def delete_all(user_id: int) -> None:
    """Идущие ролики снимаются у шлюза, затем файлы, затем строки. Файл, который не удалился,
    удаление строк не останавливает: иначе история застряла бы в базе из-за одного битого файла."""
    await videos.cancel_at_gateway(user_id)
    rows = state.DB.execute("SELECT id FROM media WHERE user_id = ?", (user_id,)).fetchall()
    for row in rows:
        try:
            await media.delete_async(user_id, row["id"])
        except OSError:
            log.warning("история пользователя %d: файл media не удалился", user_id)
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
