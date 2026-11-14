"""Воспроизведения подтверждённых замечаний еженедельной проверки."""
from unittest.mock import Mock

import pytest

from studio_agent import updater
from studio_agent.errors import Refused
from test_agent import agent, root


@pytest.mark.parametrize('launched', [False, True])
async def test_failed_update_discards_stage_only_before_helper_launch(home, tmp_path, launched):
    discard = Mock()
    launch = Mock(side_effect=None if launched else OSError())
    quit = Mock(side_effect=RuntimeError())
    update = updater.Updater(home, lambda: False, quit, prepare_fn=Mock(return_value=tmp_path),
                             launch_fn=launch, discard_fn=discard)
    update.start({'version': '99.0.0', 'sha256': 'a' * 64, 'size': 1})
    await update.task
    assert update.state == 'failed'
    assert discard.call_count == (0 if launched else 1)


def test_bad_answer_decision_has_a_protocol_error(agent):
    with pytest.raises(Refused, match='invalid'):
        agent.runner.answer('p', 'c', 'r', [], None, None)
