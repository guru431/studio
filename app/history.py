"""Вся история пользователя: чаты с сообщениями, вложения, картинки, ролики, расшифровки, голоса,
озвучки, отметки чатов и рабочее место Claude Code и файлы media."""
import asyncio
import logging

import chat
import images
import media
import ocr
import speeches
import state
import transcripts
import translate
import videos

log = logging.getLogger("studio")


async def stop_user(user_id: int) -> None:
    """Всё идущее у пользователя останавливается: задания картинок (картинку, пришедшую после,
    задание выбросит само и освободит карту), ответы чата, ролики у шлюза. Стирание истории,
    удаление и отключение учётки."""
    images.forget_user(user_id)
    await chat.stop_user(user_id)
    await videos.cancel_user(user_id)
    await transcripts.stop_user(user_id)
    await translate.stop_user(user_id)           # раньше ocr: перевод, ждущий скан, не начнёт следующее
    await ocr.stop_user(user_id)
    await speeches.stop_user(user_id)


async def delete_all(user_id: int) -> int:
    """Идущее останавливается (stop_user), затем файлы (одним вызовом в потоке на проход), затем
    строки. Пока поток удаляет файлы, могут появиться новые строки media (вложение из другой вкладки,
    ролик наблюдателя, задание, начатое посреди стирания): проходы повторяются, пока новых нет, и от
    последней проверки до COMMIT нет await — строка, вставленная позже, найдёт своего родителя
    удалённым и выбросит файл сама. Файл, который не удалился, удаление строк не останавливает
    (иначе история застряла бы в базе из-за одного битого файла): его путь уходит в очередь удаления
    той же транзакцией, что и строки, и доудалится, когда шара вернётся. Сначала — проход очереди
    пользователя: оставшееся от прежних удалений. Итог — сколько его файлов ещё ждут в очереди."""
    await stop_user(user_id)
    await media.purge(user_id)
    tried: set[str] = set()
    failed = []
    while rows := [(r["id"], r["rel_path"]) for r in
                   state.DB.execute("SELECT id, rel_path FROM media WHERE user_id = ?", (user_id,))
                   if r["id"] not in tried]:
        failed += await asyncio.to_thread(media.remove_files, rows)
        tried.update(media_id for media_id, _ in rows)
    if failed:
        log.warning("история пользователя %d: не удалилось файлов media — %d, ждут в очереди", user_id, len(failed))
    images.forget_user(user_id)                   # и задания, начатые, пока удалялись файлы
    state.DB.execute("BEGIN")
    try:
        media.queue(user_id, failed)
        state.DB.execute("DELETE FROM attachments WHERE user_id = ?", (user_id,))
        state.DB.execute("DELETE FROM chats WHERE user_id = ?", (user_id,))      # сообщения — каскадом
        # scan_pages и translation_parts — каскадом; code_chats — отметки студии, сессии остаются на машинах;
        # code_tabs и code_place — рабочее место раздела: номера сессий — тоже история (спека code-workspace 6.8);
        # agents, agent_events и push_subs — не история (спека claude-code 6.1)
        for table in ("images", "videos", "transcripts", "voices", "speeches", "scans", "translations", "code_chats",
                      "code_tabs", "code_place", "media"):
            state.DB.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    # и ответ хода без фото, начатого, пока удалялись файлы: его диалога уже нет, а модель держала бы
    # карту до конца ответа, который некуда сохранить
    await chat.stop_user(user_id)
    log.info("история пользователя %d удалена", user_id)
    return media.pending(user_id)
