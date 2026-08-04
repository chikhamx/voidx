from __future__ import annotations

import pytest

from voidx.agent.application.autonomous import AutonomousServiceBase


class _Scheduler:
    pass


class _ConcreteService(AutonomousServiceBase[str, _Scheduler]):
    def _spec_thread_id(self, spec: str, parent: str) -> str:
        return f"{parent}:{spec}"

    def _register_thread(self, thread_id: str) -> None:
        return None

    def _unregister_thread(self, thread_id: str) -> None:
        return None



def test_autonomous_service_base_requires_lifecycle_hooks(tmp_path):
    with pytest.raises(TypeError, match="abstract"):
        AutonomousServiceBase(store=object(), scheduler=object(), workspace=str(tmp_path))

    service = _ConcreteService(store=object(), scheduler=_Scheduler(), workspace=str(tmp_path))
    assert service._spec_thread_id("goal", "parent") == "parent:goal"

