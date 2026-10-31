"""Цепочка промптов картинки и ролика (спека 2026-10-20, решение 11, раздел 6.6).

Шаг — словарь: `kind` (upload / generation / edit / brush / video) и что известно о шаге — `image_id`
или `video_id`, `media_id`, `model`, `prompt`, `size`, `seed`, `strength`, `created`, у ролика ещё
`seconds`, `quality`, `prompt_source`. Цепочка — снимок на момент создания (столбец `chain`):
цепочка исходника плюс свой шаг, поэтому удалённая промежуточная картинка не уносит промпт. Старые
строки (`chain` NULL) собираются по ссылкам `source_media_id` → картинка с таким `media_id`.

Читаются только строки этого `user_id`: чужой исходник — шаг `upload` без промпта, а его файл в
ответе не называется (`annotate`). В журнал ничего. Только синхронное чтение базы: create_job и
create_video зовут of_source без `await` между проверкой исходника и INSERT.
"""
import json

import state

MAX_DEPTH = 50                     # шагов, собранных по ссылкам у старой строки


def image_step(*, image_id: str, kind: str, model: str, prompt: str, size: str, seed: int,
               strength: "float | None", created: float) -> dict:
    return {"kind": kind, "image_id": image_id, "model": model, "prompt": prompt, "size": size, "seed": seed,
            "strength": strength, "created": created}


def video_step(*, video_id: str, prompt: str, prompt_source: "str | None", seconds: int, quality: str,
               created: float) -> dict:
    return {"kind": "video", "video_id": video_id, "prompt": prompt, "prompt_source": prompt_source,
            "seconds": seconds, "quality": quality, "created": created}


def _image_row(user_id: int, media_id: str):
    return state.DB.execute("SELECT * FROM images WHERE user_id = ? AND media_id = ?", (user_id, media_id)).fetchone()


def of_source(user_id: int, media_id: "str | None") -> list[dict]:
    """Цепочка исходника: картинка истории этого пользователя — её цепочка, прочее — шаг `upload`."""
    if media_id is None:
        return []
    row = _image_row(user_id, media_id)
    return of_image(user_id, row) if row is not None else [{"kind": "upload", "media_id": media_id}]


def _own_image_step(row) -> dict:
    kind = "brush" if row["mask_media_id"] else "edit" if row["source_media_id"] else "generation"
    return image_step(image_id=row["id"], kind=kind, model=row["model"], prompt=row["prompt"], size=row["size"],
                      seed=row["seed"], strength=row["strength"], created=row["created"])


def _finish(chain: list[dict], **last) -> list[dict]:
    """Последний шаг — со значениями строки: картинка и seed приходят после создания."""
    return [*chain[:-1], {**chain[-1], **last}]


def of_image(user_id: int, row) -> list[dict]:
    if row["chain"] is not None:
        return _finish(json.loads(row["chain"]), media_id=row["media_id"], seed=row["seed"])
    # старая строка: назад по ссылкам до сохранённой цепочки, загрузки, круга или MAX_DEPTH
    tail, seen, prefix = [], set(), []
    while True:
        tail.append({**_own_image_step(row), "media_id": row["media_id"]})
        seen.add(row["id"])
        source = row["source_media_id"]
        if source is None or len(tail) >= MAX_DEPTH:
            break
        parent = _image_row(user_id, source)
        if parent is None:
            prefix = [{"kind": "upload", "media_id": source}]
            break
        if parent["id"] in seen:
            break
        if parent["chain"] is not None:
            prefix = of_image(user_id, parent)
            break
        row = parent
    return prefix + tail[::-1]


def of_video(user_id: int, row) -> list[dict]:
    last = {"media_id": row["media_id"], "prompt": row["prompt"], "prompt_source": row["prompt_source"]}
    if row["chain"] is not None:
        return _finish(json.loads(row["chain"]), **last)
    own = video_step(video_id=row["id"], prompt=row["prompt"], prompt_source=row["prompt_source"],
                     seconds=row["seconds"], quality=row["quality"], created=row["created"])
    return of_source(user_id, row["source_media_id"]) + [{**own, **last}]


def video_counts(user_id: int, rows) -> dict[str, int]:
    """Число шагов ленты без полных цепочек; предки старых роликов — одним запросом, только свои."""
    counts = {r["id"]: len(json.loads(r["chain"])) for r in rows if r["chain"] is not None}
    legacy = [r for r in rows if r["chain"] is None]
    sources = list({r["source_media_id"] for r in legacy if r["source_media_id"] is not None})
    parents = {}
    if sources:
        marks = ",".join("?" for _ in sources)
        ancestors = state.DB.execute(
            "WITH RECURSIVE ancestors(id, media_id, source_media_id, steps, depth) AS ("
            " SELECT id, media_id, source_media_id, json_array_length(chain), 1 FROM images"
            f" WHERE user_id = ? AND media_id IN ({marks})"
            " UNION SELECT i.id, i.media_id, i.source_media_id, json_array_length(i.chain), a.depth + 1"
            " FROM images i JOIN ancestors a ON i.media_id = a.source_media_id"
            " WHERE i.user_id = ? AND a.steps IS NULL AND a.depth < ?)"
            " SELECT id, media_id, source_media_id, steps FROM ancestors",
            (user_id, *sources, user_id, MAX_DEPTH))
        parents = {r["media_id"]: r for r in ancestors}
    for row in legacy:
        count, source, seen = 1, row["source_media_id"], set()  # свой шаг ролика
        while source is not None:
            parent = parents.get(source)
            if parent is None:
                count += 1                                  # загрузка с устройства
                break
            if parent["id"] in seen:
                break
            if parent["steps"] is not None:
                count += parent["steps"]
                break
            count += 1
            seen.add(parent["id"])
            if len(seen) >= MAX_DEPTH:
                break
            source = parent["source_media_id"]
        counts[row["id"]] = count
    return counts


def _exists(table: str, user_id: int, key: str, value: str) -> bool:
    # table и key — из кода этого модуля, не из запроса
    return state.DB.execute(f"SELECT 1 FROM {table} WHERE {key} = ? AND user_id = ?", (value, user_id)).fetchone() is not None


def annotate(user_id: int, chain: list[dict]) -> list[dict]:
    """Копия для ответа API: `gone` — картинки или ролика шага больше нет; `media_id` — только своего
    живого файла."""
    out = []
    for step in chain:
        step = dict(step)
        if "image_id" in step:
            step["gone"] = not _exists("images", user_id, "id", step["image_id"])
        elif "video_id" in step:
            step["gone"] = not _exists("videos", user_id, "id", step["video_id"])
        if "media_id" in step and not (step["media_id"] and _exists("media", user_id, "id", step["media_id"])):
            del step["media_id"]
        out.append(step)
    return out
