"""Профилировщик стандартной библиотеки и профиль учётки не подменяют друг друга."""
import os
import subprocess
import sys

import pytest

from conftest import APP


@pytest.mark.integration
def test_application_imports_after_the_standard_profiler(env):
    result = subprocess.run([sys.executable, '-c',
        'import cProfile; import account_profile; import main; import profile; '
        'assert hasattr(profile, "Profile"); assert account_profile.router; assert main.app'],
        cwd=APP, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
