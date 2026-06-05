"""Tests for codepilot config init and config validate commands."""

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]
from pathlib import Path
import tempfile
from unittest.mock import patch

from click.testing import CliRunner


class TestConfigInit:
    """Test config init command."""

    def test_config_init_help(self):
        """Test config init --help."""
        from codepilot.commands.config_cmd import config_group

        runner = CliRunner()
        result = runner.invoke(config_group, ["init", "--help"])
        assert result.exit_code == 0
        assert "交互式初始化" in result.output

    def test_config_init_non_interactive(self):
        """Test config init with non-interactive mode."""
        from codepilot.commands.config_cmd import config_group

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = CliRunner()
            result = runner.invoke(
                config_group,
                ["init", "--path", tmpdir, "--non-interactive"],
            )

            assert result.exit_code == 0, result.output
            assert (Path(tmpdir) / "AGENTS.toml").exists()
            parsed = tomllib.loads((Path(tmpdir) / "AGENTS.toml").read_text(encoding="utf-8"))
            assert parsed["automation"]["agent_input_language"] == "en"
            assert parsed["automation"]["agent_output_language"] == "zh-CN"

    def test_config_init_non_interactive_refuses_to_overwrite_existing_config(self):
        """Test config init --non-interactive does not clobber existing config."""
        from codepilot.commands.config_cmd import config_group

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "AGENTS.toml"
            original = '[project]\nname = "sentinel"\nbase_branch = "dev"\n'
            config_path.write_text(original, encoding="utf-8")

            runner = CliRunner()
            result = runner.invoke(
                config_group,
                ["init", "--path", tmpdir, "--non-interactive"],
            )

            assert result.exit_code == 1
            assert "已存在" in result.output
            assert config_path.read_text(encoding="utf-8") == original

    def test_config_init_moves_interactive_provider_api_key_to_secrets(self):
        """Test interactive init does not persist API keys in AGENTS.toml."""
        from codepilot.commands.config_cmd import config_group
        from codepilot.core.config import SECRETS_FILENAME

        user_input = "\n".join(
            [
                "demo",
                "main",
                "codex",
                "2",
                "y",
                "sk-review-secret",
                "gpt-test",
                "",
                "0",
                "dual",
                "branch",
                "y",
                "n",
            ]
        ) + "\n"

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = CliRunner()
            with patch("shutil.which", return_value=None):
                result = runner.invoke(config_group, ["init", "--path", tmpdir], input=user_input)

            assert result.exit_code == 0, result.output
            agents_text = (Path(tmpdir) / "AGENTS.toml").read_text(encoding="utf-8")
            secrets_text = (Path(tmpdir) / SECRETS_FILENAME).read_text(encoding="utf-8")
            secrets_data = tomllib.loads(secrets_text)
            assert "sk-review-secret" not in agents_text
            assert secrets_data["providers"]["openai"]["api_key"] == "sk-review-secret"

    def test_config_init_path_required(self):
        """Test config init requires path or global."""
        from codepilot.commands.config_cmd import config_group

        runner = CliRunner()
        # Without --path or --global, should ask for input or fail gracefully
        result = runner.invoke(
            config_group,
            ["init", "--non-interactive"],
            input="\n",
        )
        # Should not crash
        assert result.exit_code in (0, 1)


class TestConfigValidate:
    """Test config validate command."""

    def test_config_validate_help(self):
        """Test config validate --help."""
        from codepilot.commands.config_cmd import config_group

        runner = CliRunner()
        result = runner.invoke(config_group, ["validate", "--help"])
        assert result.exit_code == 0
        assert "验证" in result.output

    def test_config_validate_missing_file(self):
        """Test config validate with missing config file."""
        from codepilot.commands.config_cmd import config_group

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = CliRunner()
            # Use positional argument, not --path option
            result = runner.invoke(config_group, ["validate", tmpdir])

            # Should report file not found (exit code 1 or 2)
            assert result.exit_code in (1, 2)

    def test_config_validate_valid_config(self):
        """Test config validate with valid config."""
        from codepilot.commands.config_cmd import config_group
        from codepilot.commands.config_cmd import render_agents_toml, _canonical_config

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "AGENTS.toml"

            # Create a valid config
            data = {
                "project": {"name": "test-project", "base_branch": "main"},
            }
            canonical = _canonical_config(data, project_name="test-project")
            content = render_agents_toml(canonical)
            config_path.write_text(content, encoding="utf-8")

            runner = CliRunner()
            # Use positional argument, not --path option
            result = runner.invoke(config_group, ["validate", tmpdir])

            # Should pass validation
            assert result.exit_code == 0

    def test_config_validate_invalid_toml(self):
        """Test config validate with invalid TOML."""
        from codepilot.commands.config_cmd import config_group

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "AGENTS.toml"
            config_path.write_text("invalid toml [[[", encoding="utf-8")

            runner = CliRunner()
            # Use positional argument, not --path option
            result = runner.invoke(config_group, ["validate", tmpdir])

            # Should fail with exit code 1 (TOML parse error)
            assert result.exit_code == 1

    def test_config_validate_reports_invalid_task_workspace(self):
        """Test config validate reports raw invalid automation.task_workspace."""
        from codepilot.commands.config_cmd import config_group

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "AGENTS.toml"
            config_path.write_text(
                '[project]\nname = "demo"\nbase_branch = "main"\n\n'
                '[automation]\ntask_workspace = "invalid"\n',
                encoding="utf-8",
            )

            runner = CliRunner()
            result = runner.invoke(config_group, ["validate", tmpdir])

            assert result.exit_code == 1
            assert "automation.task_workspace" in result.output

    def test_config_validate_reports_invalid_preflight_dirty_worktree(self):
        """Test config validate reports raw invalid automation.preflight_dirty_worktree."""
        from codepilot.commands.config_cmd import config_group

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "AGENTS.toml"
            config_path.write_text(
                '[project]\nname = "demo"\nbase_branch = "main"\n\n'
                '[automation]\npreflight_dirty_worktree = "archive"\n',
                encoding="utf-8",
            )

            runner = CliRunner()
            result = runner.invoke(config_group, ["validate", tmpdir])

            assert result.exit_code == 1
            assert "automation.preflight_dirty_worktree" in result.output

    def test_config_validate_reports_invalid_agent_input_language(self):
        """Test config validate reports raw invalid automation.agent_input_language."""
        from codepilot.commands.config_cmd import config_group

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "AGENTS.toml"
            config_path.write_text(
                '[project]\nname = "demo"\nbase_branch = "main"\n\n'
                '[automation]\nagent_input_language = "fr"\n',
                encoding="utf-8",
            )

            runner = CliRunner()
            result = runner.invoke(config_group, ["validate", tmpdir])

            assert result.exit_code == 1
            assert "agent_input_language" in result.output
