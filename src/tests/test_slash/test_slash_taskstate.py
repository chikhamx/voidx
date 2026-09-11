import inspect

from voidx.agent.adapters import slash_host
from voidx.presentation.slash import port
from voidx.presentation.slash.commands.mode import ModeCommandsMixin
from voidx.presentation.slash.handler import SlashHandler
from voidx.presentation.slash.registry import SLASH_COMMANDS


def test_taskstate_experiment_is_not_registered():
    assert all(command.name != "/taskstate" for command in SLASH_COMMANDS)
    assert not hasattr(ModeCommandsMixin, "_taskstate")


def test_taskstate_experiment_has_no_dedicated_ports():
    assert not hasattr(port, "TaskStateSlashPort")
    assert not hasattr(slash_host, "TaskStateSlashAdapter")
    assert "task_state_port" not in inspect.signature(SlashHandler).parameters
