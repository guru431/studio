"""Вся история пользователя: чаты с сообщениями, вложения, картинки, ролики, расшифровки, голоса,
озвучки, отметки чатов и рабочее место Claude Code и файлы media."""
import asyncio
import logging

import chat
import images
import media
import ocr
import operations
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
    with operations.barrier(user_id):
        await _stop_user(user_id)


async def _stop_user(user_id: int) -> None:
    images.forget_user(user_id)
    await chat.stop_user(user_id)
    await videos.cancel_user(user_id)
    await transcripts.stop_user(user_id)
    await translate.stop_user(user_id)           # раньше ocr: перевод, ждущий скан, не начнёт следующее
    await ocr.stop_user(user_id)
    await speeches.stop_user(user_id)


async def delete_all(user_id: int) -> int:
    """Закрыть поколение создания: начатые запросы и фоновые задачи не смогут
    вернуть историю после очистки, новые не смогут писать до её окончания."""
    with operations.barrier(user_id):
        return await _delete_all(user_id)


async def _delete_all(user_id: int) -> int:
    """Идущее останавливается, затем удаляются файлы (в потоке), затем строки.
    Файл, который не удалился, удаление строк не останавливает
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
        # agents, agent_events и push_subs — не история (спека claude-code 6.1); security_events — тоже:
        # след входов в учётку, стирание истории пароля не требует (security_log.py)
        for table in ("chat_shares", "images", "videos", "transcripts", "voices", "speeches", "scans", "translations", "code_chats",
                      "code_tabs", "code_member_tabs", "code_place", "media"):
            state.DB.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        # отметки «завершено, не прочитано» — тоже номера сессий (своего user_id нет — по компьютерам учётки);
        # и ссылки «Поделиться» на чаты её компьютеров, кем бы ни созданы (участник проекта — прежним кодом)
        for table in ("code_attention", "chat_shares"):
            state.DB.execute(f"DELETE FROM {table} WHERE agent_id IN (SELECT id FROM agents WHERE user_id = ?)",
                             (user_id,))
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    # и ответ хода без фото, начатого, пока удалялись файлы: его диалога уже нет, а модель держала бы
    # карту до конца ответа, который некуда сохранить
    await chat.stop_user(user_id)
    log.info("история пользователя %d удалена", user_id)
    return media.pending(user_id)
