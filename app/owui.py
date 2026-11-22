"""Учётки Open WebUI в учётке Студии (спека 2026-11-22-studio-open-webui-design).

Заготовка задачи 2: `Account` — то, что нужно `owui_tokens` и `upstream`; хранение, маршруты и REST-обёртка — в задаче 3.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    id: int
    user_id: int
    label: str
    base_url: str
    cert_pem: str | None
    state: str

    @property
    def name(self) -> str:
        return self.label
