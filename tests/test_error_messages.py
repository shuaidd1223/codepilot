"""Tests for codepilot.core.error_messages module."""

from codepilot.core.error_messages import format_error, suggest_fix
from codepilot.core.config import ConfigError as RuntimeConfigError
from codepilot.errors import CodePilotError, ConfigError, ProviderError


class TestFormatError:
    """Test format_error() function."""

    def test_config_error(self):
        """Test format_error() for ConfigError."""
        exc = ConfigError("AGENTS.toml 配置错误：缺少 name 字段")
        result = format_error(exc, context="test")
        assert "配置" in result
        assert "codepilot doctor" in result

    def test_runtime_config_error(self):
        """Test format_error() for the ConfigError raised by config parsing."""
        exc = RuntimeConfigError("AGENTS.toml 配置错误：缺少 name 字段")
        result = format_error(exc, context="test")
        assert "配置" in result
        assert "codepilot config init" in result

    def test_provider_error(self):
        """Test format_error() for ProviderError."""
        exc = ProviderError("API Key 未配置")
        result = format_error(exc, context="test")
        assert "API" in result or "provider" in result.lower()

    def test_codepilot_error(self):
        """Test format_error() for other CodePilotError subclasses."""
        exc = CodePilotError("Some error")
        result = format_error(exc, context="test")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_unknown_error(self):
        """Test format_error() for unknown exceptions."""
        exc = ValueError("Unknown error")
        result = format_error(exc, context="test")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_with_empty_context(self):
        """Test format_error() with empty context."""
        exc = ConfigError("Config error")
        result = format_error(exc)
        assert isinstance(result, str)


class TestSuggestFix:
    """Test suggest_fix() function."""

    def test_suggest_config_error_fix(self):
        """Test suggest_fix() for ConfigError."""
        exc = ConfigError("AGENTS.toml 配置错误")
        suggestions = suggest_fix(exc)
        assert isinstance(suggestions, list)
        assert len(suggestions) > 0
        assert any("doctor" in s for s in suggestions)

    def test_suggest_provider_error_fix(self):
        """Test suggest_fix() for ProviderError."""
        exc = ProviderError("API Key 未配置")
        suggestions = suggest_fix(exc)
        assert isinstance(suggestions, list)
        assert len(suggestions) > 0

    def test_suggest_generic_error_fix(self):
        """Test suggest_fix() for generic exceptions."""
        exc = FileNotFoundError("File not found")
        suggestions = suggest_fix(exc)
        assert isinstance(suggestions, list)
