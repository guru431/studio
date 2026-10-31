"""Правки из протоколов CLI; файлы не читаются, отсутствующий старый текст не выдумывается."""

MAX_CHARS = 12_000
MAX_FILES = 8


def change(path, *, before=None, after=None, patch=None):
    if not isinstance(path, str) or not path:
        return None
    row = {"path": path[:1024]}
    for key, value in (("before", before), ("after", after), ("patch", patch)):
        if isinstance(value, str):
            row[key] = value[:MAX_CHARS]
            if len(value) > MAX_CHARS:
                row["truncated"] = True
    return row if len(row) > 1 else None


def for_tool(tool, data):
    if not isinstance(data, dict):
        return []
    path = data.get("file_path") or data.get("path")
    if tool in ("Edit", "edit"):
        row = change(path, before=data.get("old_string", data.get("oldText")),
                     after=data.get("new_string", data.get("newText")))
    elif tool in ("Write", "write"):
        row = change(path, after=data.get("content"))
    else:
        return []
    return [row] if row else []


def codex(items):
    rows = []
    for item in items[:MAX_FILES] if isinstance(items, list) else []:
        if isinstance(item, dict):
            row = change(item.get("path"), patch=item.get("diff"))
            if row:
                rows.append(row)
    if rows and len(items) > MAX_FILES:
        rows[-1]["truncated"] = True
    return rows


def acp(content):
    rows = []
    for item in content if isinstance(content, list) else []:
        if isinstance(item, dict) and item.get("type") == "diff":
            row = change(item.get("path"), before=item.get("oldText"), after=item.get("newText"))
            if row:
                rows.append(row)
                if len(rows) >= MAX_FILES:
                    break
    return rows
