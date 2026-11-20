"""Дети pytest гаснут вместе с ним (`pytest_support.bind_children`), даже при `os._exit` по сроку теста."""
import subprocess
import sys

import pytest

from conftest import ROOT

# Внук наследует stdout ребёнка: конец трубы наступит, только когда закроются оба процесса.
CHILD = f"""
import os, subprocess, sys
sys.path.insert(0, {str(ROOT)!r})
from tools.pytest_support import bind_children
bound = bind_children()
subprocess.Popen([sys.executable, "-I", "-S", "-c", "import time; time.sleep(60)"], stdout=sys.stdout)
print("bound" if bound else "unbound", flush=True)
os._exit(3)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object — только Windows")
def test_children_die_with_crashed_pytest_process():
    out = subprocess.run([sys.executable, "-I", "-c", CHILD], stdout=subprocess.PIPE, encoding="utf-8", timeout=20)
    assert out.stdout.split() == ["bound"]
    assert out.returncode == 3                   # код выхода самого процесса сохраняется
