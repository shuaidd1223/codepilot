"""Unit tests for the CLI family registry (task #210).

The registry is the single source of truth for the claude / codex / opencode
CLI families. Both gateway resolution and the ai_support runtime layer should
read from it instead of maintaining parallel if/else ladders.
"""

from __future__ import annotations

import pytest

from codepilot.ai_support.cli_families import (
    CLI_FAMILIES,
    CLIFamily,
    all_family_names,
    get_family,
    normalize_family_name,
    planner_family_names,
)
from codepilot.ai_support.providers import CLI_COMMAND_ENV_VARS, CLI_PROVIDERS
from codepilot.ai_support.service import normalize_agent_name


def test_registry_has_three_canonical_families():
    names = set(CLI_FAMILIES.keys())
    assert names == {"claude", "codex", "opencode"}


def test_registry_entries_are_cli_family_instances():
    for fam in CLI_FAMILIES.values():
        assert isinstance(fam, CLIFamily)
        assert fam.name
        assert fam.provider_key
        assert fam.env_var.startswith("CODEPILOT_")


def test_cli_family_bundled_path_defaults_are_backward_compatible():
    family = CLIFamily(
        name="custom",
        provider_key="custom",
        env_var="CODEPILOT_CUSTOM_CMD",
    )

    assert family.bundled_path == ""


def test_cli_family_bundled_paths_match_vendor_manifest_layout():
    assert CLI_FAMILIES["claude"].bundled_path == ""
    assert CLI_FAMILIES["codex"].bundled_path == "bin/vendor/codex"
    assert CLI_FAMILIES["opencode"].bundled_path == "bin/vendor/opencode"


def test_get_family_canonical_name():
    fam = get_family("claude")
    assert fam is not None
    assert fam.name == "claude"
    assert fam.provider_key == "claude"


def test_get_family_alias_lookup():
    fam_oc = get_family("oc")
    assert fam_oc is not None
    assert fam_oc.name == "opencode"


def test_get_family_unknown_returns_none():
    assert get_family("not-a-real-family") is None
    assert get_family("") is None


def test_normalize_family_name_handles_alias_and_canonical():
    assert normalize_family_name("oc") == "opencode"
    assert normalize_family_name("OPENCODE") == "opencode"
    assert normalize_family_name("codex") == "codex"


def test_planner_family_names_includes_all_three():
    planners = planner_family_names()
    assert {"claude", "codex", "opencode"}.issubset(planners)


def test_all_family_names_returns_canonical_list():
    assert sorted(all_family_names()) == ["claude", "codex", "opencode"]


def test_cli_providers_registers_opencode():
    assert "opencode" in CLI_PROVIDERS
    provider = CLI_PROVIDERS["opencode"]
    # OpenCode headless invocation must include the prompt placeholder so the
    # subprocess wiring can substitute the user's prompt.
    flat_template = " ".join(provider.args_template)
    assert "{prompt}" in flat_template


def test_cli_command_env_vars_includes_all_families():
    for family_name, family in CLI_FAMILIES.items():
        assert CLI_COMMAND_ENV_VARS.get(family_name) == family.env_var


def test_normalize_agent_name_recognizes_opencode_aliases():
    assert normalize_agent_name("opencode") == "opencode"
    assert normalize_agent_name("oc") == "opencode"
    assert normalize_agent_name("OPENCODE") == "opencode"


def test_normalize_agent_name_preserves_existing_claude_codex_behaviour():
    # Regression guard: the new opencode aliases must not steal claude/codex names.
    assert normalize_agent_name("claude") == "claude"
    assert normalize_agent_name("codex") == "codex"
    assert normalize_agent_name("dual") == "dual"
