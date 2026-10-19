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
    """Идущие задания и ответы останавливаются, ролики снимаются у шлюза, затем файлы (одним
    вызовом в потоке на проход), затем строки. Пока поток удаляет файлы, могут появиться новые
    строки media (вложение из другой вкладки, ролик наблюдателя, задание, начатое посреди
    стирания): проходы повторяются, пока новых нет, и от последней проверки до COMMIT нет await —
    строка, вставленная позже, найдёт своего родителя удалённым и выбросит файл сама. Файл, который
    не удалился, удаление строк не останавливает: иначе история застряла бы в базе из-за одного
    битого файла."""
    images.forget_user(user_id)                   # картинку, пришедшую после, задание выбросит само
    await chat.stop_user(user_id)
    await videos.cancel_at_gateway(user_id)
    tried: set[str] = set()
    failed = 0
    while rows := [(r["id"], r["rel_path"]) for r in
                   state.DB.execute("SELECT id, rel_path FROM media WHERE user_id = ?", (user_id,))
                   if r["id"] not in tried]:
        failed += await asyncio.to_thread(media.remove_files, rows)
        tried.update(media_id for media_id, _ in rows)
    if failed:
        log.warning("история пользователя %d: не удалилось файлов media — %d", user_id, failed)
    images.forget_user(user_id)                   # и задания, начатые, пока удалялись файлы
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
