"""HITL DTO contracts, independent of runtime coordination."""

import pytest
from pydantic import ValidationError

from voidx.tooling.domain import interaction
from voidx.tooling.domain.ui_events import (
    CheckpointPlanPayload,
    ChoicePayload,
    GoalSpecPayload,
    LoopSpecPayload,
)


def request(**overrides):
    return interaction.InteractionRequest.model_validate({
        "interaction_id": "interaction-1",
        "session_id": "session-1",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "input_kind": "choice",
        "purpose": "generic",
        "prompt": "Choose",
        **overrides,
    })


def test_request_defaults():
    dto = request()
    assert dto.timeout == 120.0
    assert dto.choices == []
    assert dto.allow_free_text is False
    assert dto.default_value == ""
    assert dto.secret is False
    assert dto.tools == []
    assert dto.allowed_scopes == []
    assert dto.checkpoint is dto.goal is dto.loop is None


@pytest.mark.parametrize("field", ["interaction_id", "session_id", "thread_id", "turn_id"])
@pytest.mark.parametrize("value", [None, "", " \t\n"])
def test_request_requires_nonblank_ownership(field, value):
    with pytest.raises(ValidationError):
        request(**{field: value})


@pytest.mark.parametrize("field", ["interaction_id", "session_id", "thread_id", "turn_id"])
def test_request_requires_ownership_fields(field):
    data = request().model_dump()
    del data[field]
    with pytest.raises(ValidationError):
        interaction.InteractionRequest.model_validate(data)


@pytest.mark.parametrize("timeout", [None, 0, -1, float("inf"), float("-inf"), float("nan")])
def test_timeout_must_be_finite_positive(timeout):
    with pytest.raises(ValidationError):
        request(timeout=timeout)


def test_timeout_can_be_overridden():
    assert request(timeout=0.5).timeout == 0.5


@pytest.mark.parametrize("input_kind", ["choice", "text", "permission"])
@pytest.mark.parametrize("purpose", ["permission", "checkpoint", "clarify", "goal", "loop", "generic"])
def test_input_kind_and_purpose_are_separate_axes(input_kind, purpose):
    dto = request(input_kind=input_kind, purpose=purpose)
    assert dto.input_kind == input_kind
    assert dto.purpose == purpose


@pytest.mark.parametrize("field", ["input_kind", "purpose"])
def test_unknown_kind_or_purpose_is_rejected(field):
    with pytest.raises(ValidationError):
        request(**{field: "unknown"})


def test_named_choices_and_text_settings_round_trip():
    dto = request(
        choices=[{"label": "Accept", "value": "yes", "description": "Proceed"}],
        allow_free_text=True,
        default_value="initial answer",
        secret=True,
    )
    assert dto.choices == [ChoicePayload(label="Accept", value="yes", description="Proceed")]
    assert dto.allow_free_text and dto.secret
    assert dto.default_value == "initial answer"
    assert interaction.InteractionRequest.model_validate_json(dto.model_dump_json()) == dto


@pytest.mark.parametrize("choice", ["yes", ("Accept", "yes", "Proceed"), {"label": "Accept"}])
def test_new_choices_require_named_fields(choice):
    with pytest.raises(ValidationError):
        request(choices=[choice])


def test_permission_details_and_scopes_round_trip():
    tool = {
        "name": "bash", "pattern": "git *", "args": {"command": "git status"},
        "risk": {"level": "low"}, "allowed_scopes": ["once", "session"],
        "default_scope": "once",
    }
    dto = request(input_kind="permission", purpose="permission", tools=[tool],
                  allowed_scopes=["once", "session"])
    assert isinstance(dto.tools[0], interaction.InteractionPermissionTool)
    assert dto.tools[0].model_dump() == tool
    assert dto.allowed_scopes == ["once", "session"]
    assert interaction.InteractionRequest.model_validate_json(dto.model_dump_json()) == dto


@pytest.mark.parametrize("field,value", [("name", " "), ("allowed_scopes", [""]), ("default_scope", " ")])
def test_permission_identifiers_are_nonblank(field, value):
    with pytest.raises(ValidationError):
        request(tools=[{"name": "bash", field: value}])


def test_allowed_scopes_are_nonblank():
    with pytest.raises(ValidationError):
        request(allowed_scopes=[" "])


@pytest.mark.parametrize("purpose,payload,model", [
    ("checkpoint", {"goal": "Ship", "steps": ["Test"], "affected_files": ["a.py"], "risks": ["Regression"]}, CheckpointPlanPayload),
    ("goal", {"objective": "Ship", "acceptance_condition": "Tests pass", "achievement_method": "TDD", "max_attempts": 3}, GoalSpecPayload),
    ("loop", {"prompt": "Check status", "interval_seconds": 10}, LoopSpecPayload),
])
def test_structured_business_payloads(purpose, payload, model):
    dto = request(purpose=purpose, **{purpose: payload})
    assert isinstance(getattr(dto, purpose), model)
    assert getattr(dto, purpose).model_dump() == payload
    assert interaction.InteractionRequest.model_validate_json(dto.model_dump_json()) == dto
    with pytest.raises(ValidationError):
        request(purpose=purpose, **{purpose: "rendered text"})


@pytest.mark.parametrize("reason,decision", [
    ("answered", "approved"), ("user_rejected", "rejected"),
    ("dismissed", "skipped"), ("timed_out", "deny"),
    ("task_cancelled", "cancelled"),
])
def test_resolution_keeps_decision_and_reason_explicit(reason, decision):
    dto = interaction.InteractionResolution(decision=decision, resolution_reason=reason)
    assert dto.value == ""
    assert dto.free_text is False
    assert dto.decision == decision
    assert dto.resolution_reason == reason
    assert interaction.InteractionResolution.model_validate_json(dto.model_dump_json()) == dto
    assert interaction.UserResponse(value=dto.value, free_text=dto.free_text).value == ""


def test_resolution_preserves_external_answer():
    dto = interaction.InteractionResolution(value="adjust scope", free_text=True,
                                            decision="modified", resolution_reason="answered")
    assert dto.value == "adjust scope"
    assert dto.free_text is True


@pytest.mark.parametrize("overrides", [
    {"resolution_reason": "unknown"}, {"value": None}, {"decision": " "},
])
def test_invalid_resolution(overrides):
    with pytest.raises(ValidationError):
        interaction.InteractionResolution.model_validate({
            "decision": "rejected", "resolution_reason": "dismissed", **overrides,
        })


@pytest.mark.parametrize("field", ["decision", "resolution_reason"])
def test_resolution_cannot_infer_business_decision(field):
    data = {"decision": "rejected", "resolution_reason": "dismissed"}
    del data[field]
    with pytest.raises(ValidationError):
        interaction.InteractionResolution.model_validate(data)


def test_mutable_request_defaults_are_isolated():
    first, second = request(), request()
    first.choices.append(ChoicePayload(label="Yes", value="yes"))
    first.tools.append(interaction.InteractionPermissionTool(name="bash"))
    first.allowed_scopes.append("once")
    assert second.choices == second.tools == second.allowed_scopes == []


def test_legacy_models_remain_compatible():
    dto = interaction.UserInteraction(prompt="Choose", options=["yes", ("No", "no", "Reject")])
    assert dto.timeout is None
    assert dto.options == ["yes", ("No", "no", "Reject")]
    assert interaction.UserResponse(value="").model_dump() == {
        "value": "", "cancelled": False, "free_text": False,
    }
    assert interaction.UserResponse(value="", cancelled=True).cancelled is True
    assert callable(interaction.UserInteractionCallback)


@pytest.mark.parametrize("stage,kind,id", [("decision", "choice", "interaction-1"), ("scope", "text", "scope-1")])
def test_checkpoint_stage_roundtrip(stage, kind, id):
    dto = request(purpose="checkpoint", input_kind=kind, interaction_id=id,
                  checkpoint_id="interaction-1", checkpoint_stage=stage)
    assert dto.model_dump()["checkpoint_id"] == "interaction-1"
    assert dto.model_dump()["checkpoint_stage"] == stage


@pytest.mark.parametrize("overrides", [
    dict(purpose="generic"), dict(checkpoint_id=None), dict(checkpoint_stage=None),
    dict(checkpoint_stage="other"), dict(interaction_id="other"), dict(input_kind="text"),
    dict(checkpoint_stage="scope", input_kind="text"),
    dict(checkpoint_stage="scope", interaction_id="scope", input_kind="choice"),
])
def test_checkpoint_stage_rejects_invalid_combinations(overrides):
    with pytest.raises(ValidationError):
        request(**(dict(purpose="checkpoint", checkpoint_id="interaction-1",
                        checkpoint_stage="decision") | overrides))


@pytest.mark.parametrize("values", [["later"], ["approved", "later"], ["approved", "approved"]])
def test_explicit_checkpoint_decision_rejects_unknown_or_duplicate_choices(values):
    with pytest.raises(ValidationError):
        request(purpose="checkpoint", checkpoint_id="interaction-1", checkpoint_stage="decision",
                choices=[dict(label=value, value=value) for value in values])


@pytest.mark.parametrize("values", [[], ["modified"], ["approved", "needs_doc", "modified", "rejected"]])
def test_explicit_checkpoint_decision_allows_unique_subsets(values):
    dto = request(purpose="checkpoint", checkpoint_id="interaction-1", checkpoint_stage="decision",
                  choices=[dict(label=value, value=value) for value in values])
    assert [choice.value for choice in dto.choices] == values


def test_legacy_checkpoint_unknown_choices_remain_compatible():
    assert request(purpose="checkpoint", choices=[dict(label="Later", value="later")]).choices[0].value == "later"
