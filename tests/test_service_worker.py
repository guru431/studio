"""Нажатия push: настоящий sw.js с WindowClient, ещё не готовым принимать сообщения."""
import subprocess
from pathlib import Path

import pytest

from browser import find_node


@pytest.mark.integration
def test_notification_click_navigates_to_its_project():
    node = find_node()
    if node is None:
        pytest.skip("нет node 22+")
    result = subprocess.run([node, str(Path(__file__).with_suffix(".mjs"))],
                            capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
