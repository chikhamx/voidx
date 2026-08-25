"""Goal/loop attempts pin the parent session's resolved profile snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from voidx.agent.application.agent_registry import AgentRegistry
from voidx.agent.application.automation.goal.evaluator import GoalEvaluator
from voidx.agent.application.automation.goal.goal_service import GoalService
from voidx.agent.application.automation.goal.runner import GoalRuntimeRunner
from voidx.agent.application.automation.loop.loop_service import LoopService
from voidx.agent.application.automation.loop.scheduler import LoopRuntimeRunner
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.domain.automation.goal import GOAL_PROFILE, GoalSpec, WorkCheckpoint
from voidx.agent.domain.automation.loop import LOOP_PROFILE, LoopSpec, loop_profile_for_spec
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.prompt_policy import GoalPromptPolicy, LoopPromptPolicy
from voidx.agent.domain.thread import AgentThread, LifecycleState


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    real_init = ThreadStore.__init__
    monkeypatch.setattr(
        ThreadStore,
        "__init__",
        lambda self, db_path=None: real_init(
            self, db_path=db_path if db_path is not None else tmp_path / "store.db"
        ),
    )
    # Keep AgentRegistry hermetic: no real ~/.voidx/agents leakage.
    monkeypatch.setattr(
        "voidx.agent.application.agent_registry.voidx_global_agents_dir",
        lambda: tmp_path / "no-global-agents",
    )


@dataclass
class FakeGoalScheduler:
    calls: list = field(default_factory=list)
    registered: list = field(default_factory=list)
    unregistered: list = field(default_factory=list)
    pump_starts: int = 0

    async def run_goal(self, parent_thread_id: str, spec: GoalSpec):
        self.calls.append((parent_thread_id, spec))

    def register_goal_thread(self, thread_id: str) -> None:
        self.registered.append(thread_id)

    def unregister_goal_thread(self, thread_id: str) -> None:
        self.unregistered.append(thread_id)

    def start_pump(self) -> None:
        self.pump_starts += 1


@dataclass
class FakeLoopScheduler:
    prompts: list = field(default_factory=list)
    registered: list = field(default_factory=list)
    unregistered: list = field(default_factory=list)
    pump_starts: int = 0

    async def run_prompt(self, prompt: str, *, display_text, session_id, spec):
        self.prompts.append((prompt, spec))

    def register_loop_thread(self, thread_id: str) -> None:
        self.registered.append(thread_id)

    def unregister_loop_thread(self, thread_id: str) -> None:
        self.unregistered.append(thread_id)

    def start_pump(self) -> None:
        self.pump_starts += 1


def _write_profile(workspace: Path, name: str, body: str) -> None:
    agents_dir = workspace / ".voidx" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{name}.yaml").write_text(body, encoding="utf-8")


async def _pin_parent_session(store: ThreadStore, workspace: Path, profile_id: str) -> None:
    resolved = AgentRegistry(str(workspace)).resolve(profile_id)
    session_id = "parent-session"
    await store.ensure_session(
        session_id,
        str(workspace),
        profile=profile_id,
        profile_snapshot=resolved.snapshot,
    )
    await store.create_thread(
        AgentThread(
            thread_id="parent",
            session_id=session_id,
            workspace=str(workspace),
            lifecycle=LifecycleState.READY,
        ),
        profile=resolved.runtime_profile,
    )


# ── goal attempts ────────────────────────────────────────────────────────


async def test_goal_attempt_pins_bundled_default_without_parent(tmp_path) -> None:
    store = ThreadStore()
    scheduler = FakeGoalScheduler()
    service = GoalService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    await service.start("parent", GoalSpec(objective="ship", acceptance_condition="tests pass"))

    _, spec = scheduler.calls[0]
    loaded = await store.load(spec.goal_thread_id("parent"))
    profile = loaded.profile
    assert profile.profile_id == GOAL_PROFILE.profile_id
    assert profile.name == GOAL_PROFILE.name
    assert profile.protocol == GOAL_PROFILE.protocol
    assert type(profile.prompt_policy) is GoalPromptPolicy

    binding = await store.get_goal_generation(spec.generation)
    assert binding is not None
    session = await store.get_session(binding.work_session_id)
    assert session is not None
    assert session.runtime_profile == "goal"
    assert session.profile_snapshot is not None
    assert session.profile_snapshot.source == "bundled"


async def test_goal_attempt_inherits_parent_custom_profile(tmp_path) -> None:
    _write_profile(
        tmp_path,
        "my-goal",
        "name: my-goal\nrevision: 3\nprompt_policy: goal\nrun_mode: goal_eval\nworkflow:\n  nodes:\n    - ref: review\n",
    )
    store = ThreadStore()
    await _pin_parent_session(store, tmp_path, "my-goal")
    scheduler = FakeGoalScheduler()
    service = GoalService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    await service.start("parent", GoalSpec(objective="ship", acceptance_condition="tests pass"))

    _, spec = scheduler.calls[0]
    loaded = await store.load(spec.goal_thread_id("parent"))
    assert loaded.profile.profile_id == "my-goal"
    assert loaded.profile.revision == 3
    assert loaded.profile.protocol == "goal"
    assert type(loaded.profile.prompt_policy) is GoalPromptPolicy
    assert loaded.resolved_profile.workflow_context is not None
    assert set(loaded.resolved_profile.workflow_context.dag.nodes) == {"review"}

    parent_snapshot = AgentRegistry(str(tmp_path)).resolve("my-goal").snapshot
    binding = await store.get_goal_generation(spec.generation)
    assert binding is not None
    session = await store.get_session(binding.work_session_id)
    assert session is not None
    assert session.runtime_profile == "my-goal"
    assert session.profile_snapshot is not None
    assert session.profile_snapshot.content_hash == parent_snapshot.content_hash


async def test_goal_attempt_keeps_parent_snapshot_after_file_deleted(tmp_path) -> None:
    profile_path_dir = tmp_path / ".voidx" / "agents"
    _write_profile(tmp_path, "my-goal", "name: my-goal\nrevision: 1\nprompt_policy: goal\nrun_mode: goal_eval\n")
    store = ThreadStore()
    await _pin_parent_session(store, tmp_path, "my-goal")
    parent_snapshot = AgentRegistry(str(tmp_path)).resolve("my-goal").snapshot

    # Profile file deleted after the parent session pinned its snapshot.
    (profile_path_dir / "my-goal.yaml").unlink()

    scheduler = FakeGoalScheduler()
    service = GoalService(store=store, scheduler=scheduler, workspace=str(tmp_path))
    await service.start("parent", GoalSpec(objective="ship", acceptance_condition="tests pass"))

    _, spec = scheduler.calls[0]
    binding = await store.get_goal_generation(spec.generation)
    assert binding is not None
    session = await store.get_session(binding.work_session_id)
    assert session is not None
    assert session.profile_snapshot is not None
    assert session.profile_snapshot.snapshot_hash == parent_snapshot.snapshot_hash


# ── loop attempts ────────────────────────────────────────────────────────


async def test_loop_attempt_inherits_parent_custom_profile(tmp_path) -> None:
    _write_profile(
        tmp_path,
        "my-loop",
        "name: my-loop\nrevision: 2\nprompt_policy: loop\nrun_mode: loop_dynamic\nidentity: 自定义循环助手\n",
    )
    store = ThreadStore()
    await _pin_parent_session(store, tmp_path, "my-loop")
    scheduler = FakeLoopScheduler()
    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    await service.start("parent", LoopSpec(prompt="检查构建状态"))

    _, spec = scheduler.prompts[0]
    loaded = await store.load(spec.loop_thread_id("parent"))
    profile = loaded.profile
    assert profile.profile_id == "my-loop"
    assert profile.protocol == "loop"
    assert type(profile.prompt_policy) is LoopPromptPolicy
    assert "自定义循环助手" in profile.system_prompt
    assert "检查构建状态" in profile.system_prompt

    session = await store.get_session(spec.loop_session_id("parent"))
    assert session is not None
    assert session.runtime_profile == "my-loop"
    assert session.profile_snapshot is not None
    assert session.profile_snapshot.profile_id == "my-loop"


async def test_loop_attempt_legacy_parent_matches_loop_profile_for_spec(tmp_path) -> None:
    store = ThreadStore()
    # Legacy parent: profile id "loop", no pinned snapshot.
    session_id = "legacy-parent-session"
    await store.ensure_session(str(session_id), str(tmp_path), profile="loop")
    await store.create_thread(
        AgentThread(
            thread_id="parent",
            session_id=session_id,
            workspace=str(tmp_path),
            lifecycle=LifecycleState.READY,
        ),
        profile=LOOP_PROFILE,
    )
    scheduler = FakeLoopScheduler()
    service = LoopService(store=store, scheduler=scheduler, workspace=str(tmp_path))

    await service.start("parent", LoopSpec(prompt="检查构建状态"))

    _, spec = scheduler.prompts[0]
    loaded = await store.load(spec.loop_thread_id("parent"))
    legacy = loop_profile_for_spec(spec)
    assert loaded.profile.profile_id == "loop"
    assert loaded.profile.system_prompt == legacy.system_prompt
    assert type(loaded.profile.prompt_policy) is LoopPromptPolicy

    session = await store.get_session(spec.loop_session_id("parent"))
    assert session is not None
    assert session.profile_snapshot is not None
    assert session.profile_snapshot.source == "bundled"


# ── runners consume the passed (thread-pinned) profile ──────────────────


class _RecordingRuntime:
    def __init__(self) -> None:
        self.requests = []

    async def run_turn(self, request):
        self.requests.append(request)
        return None


class _GoalEvaluator:
    def __init__(self) -> None:
        self.context = None

    def build_request(self, *, thread, context, prompt, checkpoint, guidance=()):
        self.context = context
        return GoalEvaluator().build_request(
            thread=thread,
            context=context,
            prompt=prompt,
            checkpoint=checkpoint,
            guidance=guidance,
        )


class _LoopEvents:
    def publish_message(self, message: str) -> None:
        del message

    def show_loop_waiting(self, wakeup_at: float) -> None:
        del wakeup_at

    def clear_loop_waiting(self) -> None:
        pass


async def test_goal_runner_uses_passed_profile(tmp_path) -> None:
    from voidx.agent.domain.automation.goal import GoalState

    _write_profile(
        tmp_path,
        "my-goal",
        "name: my-goal\nrevision: 1\nprompt_policy: goal\nrun_mode: goal_eval\nworkflow:\n  nodes:\n    - ref: review\n",
    )
    resolved = AgentRegistry(str(tmp_path)).resolve("my-goal")
    spec = GoalSpec(objective="ship", acceptance_condition="tests pass", generation="run-1")
    state = GoalState.from_spec(
        spec,
        run_id=spec.generation,
        main_session_id="main-profile",
        work_session_id="work-profile",
        evaluator_session_id="eval-profile",
    )
    thread = AgentThread(
        thread_id=spec.goal_thread_id("parent"),
        session_id=spec.goal_session_id("parent"),
        workspace=str(tmp_path),
        lifecycle=LifecycleState.READY,
    )
    runtime = _RecordingRuntime()
    evaluator = _GoalEvaluator()

    runner = GoalRuntimeRunner(runtime=runtime, evaluator=evaluator)
    base_frame = {
        "attempt_number": 1,
        "spec": spec.model_dump(mode="json"),
        "goal_state": state.model_dump(mode="json"),
    }
    await runner.run_turn(
        thread=thread,
        profile=resolved,
        input_frame={**base_frame, "phase": "work"},
    )
    await runner.run_turn(
        thread=thread,
        profile=resolved,
        input_frame={
            **base_frame,
            "phase": "evaluator",
            "checkpoint": WorkCheckpoint(
                generation=spec.generation,
                attempt_number=1,
                summary="work captured",
                work_turn_id="work-profile-turn",
            ).model_dump(mode="json"),
        },
    )

    work_context = runtime.requests[0].context
    assert work_context.runtime_profile is resolved.runtime_profile
    assert work_context.workflow_context is resolved.workflow_context
    assert evaluator.context.runtime_profile is resolved.runtime_profile
    assert evaluator.context.workflow_context is resolved.workflow_context
    from voidx.agent.domain.tool_policy import ProfileToolPolicy

    assert isinstance(work_context.tool_policy, ProfileToolPolicy)
    assert work_context.tool_policy.snapshot_hash == resolved.snapshot.snapshot_hash
    assert work_context.tool_policy.phase == "work"
    assert work_context.tool_policy.resource_policy is resolved.resource_policy
    assert evaluator.context.tool_policy.snapshot_hash == resolved.snapshot.snapshot_hash
    assert evaluator.context.tool_policy.phase == "evaluator"


async def test_loop_runner_uses_passed_profile(tmp_path) -> None:
    _write_profile(
        tmp_path,
        "my-loop",
        "name: my-loop\nrevision: 1\nprompt_policy: loop\nrun_mode: loop_dynamic\nworkflow:\n  nodes:\n    - ref: verify\n",
    )
    resolved = AgentRegistry(str(tmp_path)).resolve("my-loop")
    spec = LoopSpec(prompt="检查构建状态", generation="run-1")
    thread = AgentThread(
        thread_id=spec.loop_thread_id("parent"),
        session_id=spec.loop_session_id("parent"),
        workspace=str(tmp_path),
        lifecycle=LifecycleState.READY,
    )
    runtime = _RecordingRuntime()

    await LoopRuntimeRunner(runtime, _LoopEvents()).run_turn(
        thread=thread,
        profile=resolved,
        input_frame={"prompt": spec.prompt, "spec": spec.model_dump(mode="json")},
    )

    context = runtime.requests[0].context
    assert context.runtime_profile is resolved.runtime_profile
    assert context.workflow_context is resolved.workflow_context
    from voidx.agent.domain.tool_policy import ProfileToolPolicy

    assert isinstance(context.tool_policy, ProfileToolPolicy)
    assert context.tool_policy.snapshot_hash == resolved.snapshot.snapshot_hash
    assert context.tool_policy.phase == "work"
    assert context.tool_policy.resource_policy is resolved.resource_policy
