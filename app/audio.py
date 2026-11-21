"""Звук студии через ffmpeg debian (спека 2026-10-20-studio-sections-voice-design, 6.3–6.5).

- Длительность — у самого ffmpeg (`-progress pipe:1`, последнее `out_time_us`): ffprobe не нужен, и
  запись браузера (webm без длительности в заголовке) тоже меряется.
- Команды — argv-списком (`asyncio.create_subprocess_exec`, без оболочки), `-nostdin -hide_banner
  -loglevel error`, окружение — без окружения службы (`_env`). Только звук: аппаратное ускорение GPU тут ни при чём.
- Перед распознаванием запись сжимается всегда: mp3 16 кГц моно 32 кбит/с (COMPRESS) — 20 минут
  ≈ 5 МБ; это и шлюзу (slot-gate держит тело в памяти), и внешним сервисам (у OpenAI — 25 МБ).
- Временные файлы — `state.CFG.tmp_dir` (локальный диск рядом с базой, не шара): имена даёт
  `temp()`, удаляет их вызывающий в finally; при старте службы каталог чистится (`clean_tmp`).
- В журнал — только программа и код выхода: вывод ffmpeg (в нём путь к файлу) — нет. AudioError —
  слова для человека, без имён и путей.
"""
import asyncio
import logging
import os
import uuid
from asyncio.subprocess import DEVNULL, PIPE
from pathlib import Path

import state

FFMPEG = "ffmpeg"
TIMEOUT_SEC = 900
BASE = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
COMPRESS = ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k"]
WAV = ["-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le"]

log = logging.getLogger("studio")


class AudioError(Exception):
    def __init__(self, message: str = "не удалось прочитать запись"):
        super().__init__(message)


def _env() -> dict:
    """Окружение ffmpeg — по образцу web._env: без окружения службы (там STUDIO_DATA_KEY), а он разбирает
    загруженные записи. PATH — службы: по нему ищется сам ffmpeg (на машине разработки — не /usr/bin);
    Windows (тесты) без SYSTEMROOT процессы не запускает."""
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": "/nonexistent"}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


async def _run(argv: list[str]) -> bytes:
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, env=_env())
    except OSError:
        log.warning("звук: %s не запустился", argv[0])
        raise AudioError("обработка звука недоступна") from None
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), TIMEOUT_SEC)
    except BaseException as e:                   # таймаут, отмена задания, остановка службы
        proc.kill()
        await proc.wait()
        if isinstance(e, TimeoutError):
            log.warning("звук: %s дольше %d с", argv[0], TIMEOUT_SEC)
            raise AudioError("запись обрабатывается слишком долго") from None
        raise
    if proc.returncode != 0:
        log.warning("звук: %s — код %d", argv[0], proc.returncode)
        raise AudioError()
    return out


def temp(suffix: str) -> Path:
    """Новое имя временного файла."""
    return state.CFG.tmp_dir / f"{uuid.uuid4().hex}{suffix}"


def clean_tmp() -> None:
    """При старте: каталог временных файлов есть и пуст (прерванные задания оставили своё)."""
    folder = state.CFG.tmp_dir
    folder.mkdir(parents=True, exist_ok=True)
    for f in folder.iterdir():
        f.unlink(missing_ok=True)


async def probe(path: Path) -> float:
    """Длительность, с: запись декодируется в никуда, время — из отчёта `-progress`."""
    out = await _run([*BASE, "-i", str(path), "-vn", "-f", "null", "-progress", "pipe:1", "-nostats", "-"])
    times = [line.split("=", 1)[1] for line in out.decode("ascii", "replace").splitlines()
             if line.startswith("out_time_us=")]
    try:
        seconds = int(times[-1]) / 1e6
    except (IndexError, ValueError):
        raise AudioError() from None
    if seconds <= 0:
        raise AudioError("в записи нет звука")
    return seconds


async def compress(src: Path, dst: Path) -> None:
    await _run([*BASE, "-i", str(src), *COMPRESS, str(dst)])


async def cut(src: Path, start: float, length: float, dst: Path) -> None:
    """Часть записи с `start` длиной `length`, с — сжатая, как `compress` (длинная «Расшифровка» частями)."""
    await _run([*BASE, "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src), *COMPRESS, str(dst)])


async def to_wav(src: Path, dst: Path, *, max_sec: float) -> float:
    """Образец голоса: WAV PCM 16 бит моно 24 кГц, не длиннее max_sec; итог — его длительность."""
    await _run([*BASE, "-i", str(src), *WAV, "-t", f"{max_sec:.2f}", str(dst)])
    return await probe(dst)


async def mp3_of(parts: list[Path], dst: Path) -> None:
    """Части (WAV или mp3) по порядку — один mp3."""
    argv = [*BASE]
    for part in parts:
        argv += ["-i", str(part)]
    joined = "".join(f"[{i}:a]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[a]"
    await _run([*argv, "-filter_complex", joined, "-map", "[a]", "-ac", "1", "-c:a", "libmp3lame", "-q:a", "4", str(dst)])


async def file_io(fn, *args):
    """При отмене дождаться закрытия файла в потоке, прежде чем продолжить уборку."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                pass
        if not task.cancelled():
            task.exception()
        raise


async def remove_temp(*paths: Path) -> None:
    """Убрать все части, даже если во время уборки пришла ещё одна отмена."""
    def remove():
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                log.warning("звук: временный файл не удалён (%s)", type(e).__name__)

    await file_io(remove)
