from __future__ import annotations

from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "dev-bin" / "opencode-mcp-health-smoke.ps1"


def test_opencode_mcp_health_smoke_script_documents_manual_flow():
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "codepilot mcp serve --transport http --port 8767" in script
    assert "http://127.0.0.1:8767/mcp" in script
    assert "codepilot.health" in script
    assert "OPENCODE_CONFIG" in script
    assert "opencode run" in script


def test_opencode_mcp_health_smoke_script_has_clear_failure_paths():
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "Get-Command" in script
    assert "OpenCode binary was not found" in script
    assert "exit 127" in script
    assert "$LASTEXITCODE" in script
    assert "exit 1" in script


def test_opencode_mcp_health_smoke_script_has_no_machine_specific_values():
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    forbidden_fragments = [
        "C:\\Users\\",
        "D:\\",
        "sk-",
        "api_key",
        "token",
    ]
    for fragment in forbidden_fragments:
        assert fragment not in script
