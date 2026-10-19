"""Вся история пользователя: чаты с сообщениями, вложения, картинки, ролики и файлы media."""
import logging

import state

log = logging.getLogger("studio")


async def delete_all(user_id: int) -> None:
    """Сперва файлы, затем строки. Файл, который не удалился, удаление строк не останавливает:
    иначе история застряла бы в базе из-за одного битого файла."""
    rows = state.DB.execute("SELECT rel_path FROM media WHERE user_id = ?", (user_id,)).fetchall()
    for row in rows:
        try:
            (state.CFG.media_dir / row["rel_path"]).unlink(missing_ok=True)
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
