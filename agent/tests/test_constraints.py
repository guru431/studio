"""constraints.txt закрепляет всё замыкание requirements.txt моста, а не только прямые пакеты."""
import importlib.metadata
from pathlib import Path
import re

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

AGENT = Path(__file__).resolve().parents[1]
# Мост ставится на Windows x64 с Python 3.11–3.14 (install.ps1): маркер любой из этих сред тянет пакет.
WINDOWS = [{"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt", "platform_machine": "AMD64",
            "python_version": f"3.{minor}", "python_full_version": f"3.{minor}.0", "implementation_name": "cpython",
            "platform_python_implementation": "CPython"} for minor in (11, 12, 13, 14)]


def _pins() -> dict:
    pins = {}
    for line in (AGENT / "constraints.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Za-z0-9._-]+==[A-Za-z0-9.+!]+", line), line
            name, version = line.split("==")
            assert canonicalize_name(name) not in pins, line
            pins[canonicalize_name(name)] = version
    return pins


def test_constraints_pin_every_dependency_of_the_bridge():
    """Зависимости — из метаданных пакетов этой среды (requirements-dev ставит requirements.txt)."""
    pins = _pins()
    roots = [Requirement(line) for line in (AGENT / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    for req in roots:
        assert pins.get(canonicalize_name(req.name)) == str(req.specifier)[2:], req
    todo, seen, missing = list(roots), set(), set()
    while todo:
        req = todo.pop()
        name = canonicalize_name(req.name)
        if name not in pins:
            missing.add(name)
            continue
        if (name, frozenset(req.extras)) in seen:
            continue
        seen.add((name, frozenset(req.extras)))
        try:
            requires = importlib.metadata.requires(req.name) or []
        except importlib.metadata.PackageNotFoundError:
            continue
        for spec in map(Requirement, requires):
            if spec.marker is None or any(spec.marker.evaluate({**env, "extra": extra})
                                          for env in WINDOWS for extra in {"", *req.extras}):
                todo.append(spec)
    assert missing == set()
