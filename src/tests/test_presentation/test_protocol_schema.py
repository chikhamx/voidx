from __future__ import annotations

import json

from voidx.presentation.protocol import export_protocol_schema


def test_protocol_schema_exports_public_agent_profile_contracts() -> None:
    schema = export_protocol_schema()
    definitions = schema["$defs"]

    expected = {
        "AgentProfileDetailDto",
        "AgentProfileDiagnosticDto",
        "AgentProfileInfoDto",
        "AgentProfileSnapshotDto",
        "AgentProfileValidationDto",
        "AgentProfileSaveDto",
        "AgentProfileListDto",
    }
    assert expected <= set(definitions)

    info_fields = set(definitions["AgentProfileInfoDto"]["properties"])
    assert info_fields == {
        "name",
        "display_name",
        "revision",
        "content_hash",
        "source",
        "run_mode",
        "hitl_mode",
        "availability",
        "diagnostics",
    }
    snapshot_fields = set(definitions["AgentProfileSnapshotDto"]["properties"])
    assert snapshot_fields == {
        "profile_id",
        "revision",
        "source",
        "content_hash",
        "snapshot_hash",
    }

    rendered = json.dumps(schema, sort_keys=True).lower()
    assert "canonical_payload" not in rendered
    assert "system_prompt" not in rendered
    assert "file_path" not in rendered
