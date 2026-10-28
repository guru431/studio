"""Подставной мост Claude Code (протокол docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md):
привязка к учётке студии."""


def enroll(s, login="owner", name="Рабочий ПК", password="password-123", ip="10.77.48.5") -> tuple[int, str]:
    """Привязка, как её делает мост: JSON без Origin, адрес — последним в X-Forwarded-For. (agent_id, device_key)."""
    r = s.c.post("/api/agent/enroll", json={"login": login, "password": password, "name": name},
                 headers={"X-Forwarded-For": ip})
    assert r.status_code == 201, r.text
    body = r.json()
    return body["agent_id"], body["device_key"]
