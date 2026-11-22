"""Возможности моделей подключения (спека 2026-10-21-studio-connection-check-design, раздел 6).

У каждой возможности — источник: `catalog` — встроенный перечень шлюза srvllm или s06 (то, что студия знает о нём;
страница называет его по профилю паспорта),
`metadata` — `architecture` из ответа `/models` (OpenRouter и подобные), `name` — догадка по имени
(«по названию, не проверено»), `probe` — пробный ответ прошёл. Профиль — что за сервис за адресом.
Имена возможностей — слова страницы (settings.js `CAPS`).
"""
import fnmatch

import httpx

import connections
import images
import videos

PROFILES = {"api.openai.com": "openai", "openrouter.ai": "openrouter"}
ID_MAX = 200

#: встроенный перечень srvllm: короткое имя студии → возможности
CATALOG_CAPS = {
    "local-gemma-abl": ("vision",),
    "flux": ("generate", "edit", "mask", "seed"),
    "wan": ("video",),
    "h3": ("video",),
    "snofs": ("generate", "edit", "mask", "strength", "seed"),
    "fineview": ("generate", "edit", "mask", "seed"),
    "jib-moss": ("generate", "edit", "mask", "strength", "seed"),
    "whisperx": ("diarize", "dictation"),
    "gigaam": ("dictation_ru",),
    "qwen3-tts": ("voices",),
    "f5-tts": ("voices",),
}
#: перечень шлюза s06 (`sections.S06_CATALOG`): модель → возможности; WhisperX s06 делит по собеседникам
S06_CAPS = {
    "qwen-27b": ("text",),
    "qwen-27b-fast": ("text",),
    "whisperx": ("diarize", "dictation"),
    "ocr-ru": ("ocr",),
}
#: короткое имя → id шлюза в `GET /slot/models`; модели речи там не перечисляются и не сверяются
GATEWAY_IDS = {**images.MODELS, **videos.MODELS}
#: «Описать» и «EN» идут маршрутом описания модели gemma4-abl — когда `/slot/*` ключу открыты
SLOT_CAPS = {images.DESCRIPTION_MODEL: ("describe", "translate")}

#: догадки по имени (после последнего `/`, без регистра): первое совпавшее правило
NAME_RULES = (
    (("gpt-image-*", "dall-e-*"), ("generate", "edit")),
    (("whisper-*", "*-transcribe*"), ("transcribe", "dictation")),
    (("tts-*", "*-tts*"), ("tts",)),
    (("gpt-4o*", "gpt-4.1*", "gpt-5*", "o3*", "o4*", "claude-*", "gemini-*"), ("vision",)),
)


def profile(conn) -> str:
    if conn.s06:
        return "s06"
    if conn.kind == "srvllm":
        return "srvllm"
    return PROFILES.get(httpx.URL(conn.base_url).host, "compatible")


def by_name(model_id: str) -> tuple[str, ...]:
    name = model_id.rsplit("/", 1)[-1].lower()
    for patterns, found in NAME_RULES:
        if any(fnmatch.fnmatchcase(name, p) for p in patterns):
            return found
    return ()


def from_metadata(raw: dict) -> "tuple[str, ...] | None":
    """`image` на входе — смотрит фото, на выходе — рисует; None — метаданных о модальностях нет."""
    arch = raw.get("architecture")
    if not isinstance(arch, dict):
        return None
    ins, outs = arch.get("input_modalities"), arch.get("output_modalities")
    if not isinstance(ins, list) and not isinstance(outs, list):
        return None
    return tuple(c for c, have in (("vision", isinstance(ins, list) and "image" in ins),
                                   ("generate", isinstance(outs, list) and "image" in outs)) if have)


def _caps(names, source: str) -> list[dict]:
    return [{"name": n, "source": source} for n in names]


def model_entry(raw) -> "dict | None":
    """Модель из ответа `/models`: id, context_length (`connections.context_size`), возможности по метаданным или по имени."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not 0 < len(raw["id"]) <= ID_MAX:
        return None
    entry = {"id": raw["id"]}
    if (length := connections.context_size(raw)) is not None:
        entry["context_length"] = length
    meta = from_metadata(raw)
    entry["caps"] = _caps(meta, "metadata") if meta is not None else _caps(by_name(raw["id"]), "name")
    return entry


def models(kind: str, listed: list[dict], slot_ids: "list[str] | None", limit: int) -> list[dict]:
    """Модели паспорта: из `/models` (`listed` — записи model_entry), у srvllm — ещё перечень (модель
    картинок или ролика, чьего id нет в ответившем `/slot/models`, — `absent`) и прочие id шлюза, у s06 — его
    перечень S06_CAPS. `kind` — профиль подключения (`profile`). Предел режет только список сервиса: перечень
    шлюза длинный ответ не вытеснит."""
    out = {m["id"]: m for m in listed[:limit]}
    if kind == "s06":
        for name, found in S06_CAPS.items():
            out[name] = {**out.get(name, {"id": name}), "caps": _caps(found, "catalog")}
    if kind == "srvllm":
        for name, found in CATALOG_CAPS.items():
            entry = {**out.get(name, {"id": name}), "caps": _caps(found, "catalog")}
            if slot_ids is not None and name in GATEWAY_IDS and GATEWAY_IDS[name] not in slot_ids:
                entry["absent"] = True
            out[name] = entry
        if slot_ids is not None:
            for model, found in SLOT_CAPS.items():
                out.setdefault(model, {"id": model, "caps": []})["caps"] += _caps(found, "catalog")
            known = set(GATEWAY_IDS.values())
            for gid in slot_ids[:limit]:
                if gid not in known:
                    out.setdefault(gid, {"id": gid, "caps": []})
    return list(out.values())


def _probed(probe: dict) -> str:
    """Источник возможности по пробе: сервер — `probe`, браузер (его слова) — `probe_browser`."""
    return "probe_browser" if probe.get("source") == "browser" else "probe"


def with_probes(entries: list[dict], probes: dict) -> list[dict]:
    """Пробный ответ прошёл — у модели возможность `chat` с источником `probe` (или `probe_browser`)."""
    for entry in entries:
        entry["caps"] = [c for c in entry["caps"] if c["source"] not in ("probe", "probe_browser")]
        if (probes.get(entry["id"]) or {}).get("ok"):
            entry["caps"].append({"name": "chat", "source": _probed(probes[entry["id"]])})
    have = {e["id"] for e in entries}
    return entries + [{"id": m, "caps": [{"name": "chat", "source": _probed(p)}]}
                      for m, p in probes.items() if p.get("ok") and m not in have]
