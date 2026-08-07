from __future__ import annotations

import json
from pathlib import Path


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CONTRACT_FIXTURES = {
    "cli.json",
    "tool_catalog.json",
    "tool_results.json",
    "config.json",
    "providers.json",
    "slash_commands.json",
    "prompts.json",
    "state_machine.json",
}

PERSISTENCE_FIXTURES = {
    "schema_v3.json",
    "v0.db",
    "v1.db",
    "v2.db",
    "v3.db",
    "payloads.json",
}

CONTRACT_TESTS = {
    "test_cli_contract.py",
    "test_tool_contract.py",
    "test_config_contract.py",
    "test_provider_catalog_contract.py",
    "test_slash_command_contract.py",
    "test_prompt_contract.py",
    "test_state_machine_contract.py",
    "test_persistence_contract.py",
}


def test_p0_contract_assets_are_complete_and_nonempty() -> None:
    contract_dir = FIXTURES / "contracts"
    persistence_dir = FIXTURES / "persistence"
    test_dir = Path(__file__).parent

    missing = [
        *(str(contract_dir / name) for name in sorted(CONTRACT_FIXTURES) if not (contract_dir / name).is_file()),
        *(str(persistence_dir / name) for name in sorted(PERSISTENCE_FIXTURES) if not (persistence_dir / name).is_file()),
        *(str(test_dir / name) for name in sorted(CONTRACT_TESTS) if not (test_dir / name).is_file()),
    ]
    assert missing == [], "missing P0 contract assets:\n" + "\n".join(missing)

    empty = [
        str(path)
        for path in [
            *(contract_dir / name for name in CONTRACT_FIXTURES),
            persistence_dir / "schema_v3.json",
            *(test_dir / name for name in CONTRACT_TESTS),
        ]
        if path.stat().st_size == 0
    ]
    assert empty == [], "empty P0 contract assets:\n" + "\n".join(sorted(empty))

    for name in CONTRACT_FIXTURES | {"schema_v3.json"}:
        path = (contract_dir if name in CONTRACT_FIXTURES else persistence_dir) / name
        json.loads(path.read_text(encoding="utf-8"))
