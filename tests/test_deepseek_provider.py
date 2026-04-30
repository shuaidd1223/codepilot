from __future__ import annotations

import json
from copy import copy
from types import SimpleNamespace


def test_deepseek_simple_prompt_uses_configured_simple_model_without_thinking():
    from codepilot.ai_support.providers import API_PROVIDERS, _run_api_provider

    captured = {}

    class _FakeChatCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            )

    provider = copy(API_PROVIDERS["deepseek"])
    provider.api_key = "sk-test"
    provider.model = "legacy-default-should-not-be-used"
    provider.simple_model = "configured-simple-model"
    provider.complex_model = "configured-complex-model"
    provider.build_client = lambda: (
        SimpleNamespace(chat=SimpleNamespace(completions=_FakeChatCompletions())),
        "chat.completions",
    )

    assert _run_api_provider(provider, "帮我总结一下这个标题") == "ok"

    assert captured["model"] == "configured-simple-model"
    assert captured["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in captured


def test_deepseek_complex_prompt_uses_configured_complex_model_with_max_thinking():
    from codepilot.ai_support.providers import API_PROVIDERS, _run_api_provider

    captured = {}
    prompt = (
        "请重构这个多模块架构，涉及数据库迁移、兼容旧接口、并发调度、回归测试和风险边界。"
        * 80
    )

    class _FakeChatCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "total_tokens": 140,
                    "completion_tokens_details": {"reasoning_tokens": 25},
                },
            )

    provider = copy(API_PROVIDERS["deepseek"])
    provider.api_key = "sk-test"
    provider.model = "legacy-default-should-not-be-used"
    provider.simple_model = "configured-simple-model"
    provider.complex_model = "configured-complex-model"
    provider.build_client = lambda: (
        SimpleNamespace(chat=SimpleNamespace(completions=_FakeChatCompletions())),
        "chat.completions",
    )

    assert _run_api_provider(provider, prompt) == "ok"

    assert captured["model"] == "configured-complex-model"
    assert captured["thinking"] == {"type": "enabled"}
    assert captured["reasoning_effort"] == "max"


def test_openai_sync_usage_is_recorded(tmp_path, monkeypatch):
    from codepilot.ai_support.providers import API_PROVIDERS, _run_api_provider
    from codepilot.webapp.payloads import ai_status_payload

    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))

    class _FakeChatCompletions:
        def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                usage={
                    "prompt_tokens": 7,
                    "completion_tokens": 5,
                    "total_tokens": 12,
                    "prompt_cache_hit_tokens": 2,
                    "prompt_cache_miss_tokens": 5,
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            )

    provider = copy(API_PROVIDERS["deepseek"])
    provider.api_key = "sk-test"
    provider.complex_model = "configured-complex-model"
    provider.build_client = lambda: (
        SimpleNamespace(chat=SimpleNamespace(completions=_FakeChatCompletions())),
        "chat.completions",
    )

    _run_api_provider(provider, "复杂任务：数据库迁移、兼容性验证、并发调度、架构重构、回归测试。" * 20)

    payload = ai_status_payload()
    usage = payload["usage"]["deepseek"]
    assert usage["requests"] == 1
    assert usage["prompt_tokens"] == 7
    assert usage["completion_tokens"] == 5
    assert usage["total_tokens"] == 12
    assert usage["reasoning_tokens"] == 3
    assert usage["prompt_cache_hit_tokens"] == 2
    assert usage["by_model"]["configured-complex-model"]["total_tokens"] == 12


def test_openai_stream_usage_is_recorded(tmp_path, monkeypatch):
    from codepilot.ai_support.providers import API_PROVIDERS, _run_api_provider
    from codepilot.core import progress_bus
    from codepilot.webapp.payloads import ai_status_payload

    monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))

    class _FakeChatCompletions:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            assert kwargs["stream_options"] == {"include_usage": True}
            return [
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="ok"))],
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[],
                    usage={
                        "prompt_tokens": 11,
                        "completion_tokens": 4,
                        "total_tokens": 15,
                        "completion_tokens_details": {"reasoning_tokens": 2},
                    },
                ),
            ]

    provider = copy(API_PROVIDERS["deepseek"])
    provider.api_key = "sk-test"
    provider.simple_model = "configured-simple-model"
    provider.build_client = lambda: (
        SimpleNamespace(chat=SimpleNamespace(completions=_FakeChatCompletions())),
        "chat.completions",
    )

    progress_bus.clear_subscribers_for_tests()
    with progress_bus.subscription(lambda _event: None):
        assert _run_api_provider(provider, "总结这个标题") == "ok"

    usage = ai_status_payload()["usage"]["deepseek"]
    assert usage["requests"] == 1
    assert usage["prompt_tokens"] == 11
    assert usage["completion_tokens"] == 4
    assert usage["total_tokens"] == 15
    assert usage["reasoning_tokens"] == 2
    assert usage["by_model"]["configured-simple-model"]["requests"] == 1
    assert usage["by_model"]["configured-simple-model"]["total_tokens"] == 15


def test_deepseek_balance_fetches_user_balance(monkeypatch):
    from codepilot.ai_support.providers import API_PROVIDERS
    from codepilot.webapp.payloads import ai_status_payload

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "is_available": True,
                    "balance_infos": [
                        {
                            "currency": "CNY",
                            "total_balance": "12.34",
                            "granted_balance": "2.00",
                            "topped_up_balance": "10.34",
                        }
                    ],
                }
            ).encode("utf-8")

    requests = []

    def _fake_urlopen(request, timeout=0):
        requests.append((request, timeout))
        return _FakeResponse()

    provider = API_PROVIDERS["deepseek"]
    provider.api_key = "sk-test"
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    payload = ai_status_payload(refresh_balance=True)

    assert requests
    req, timeout = requests[0]
    assert req.full_url == "https://api.deepseek.com/user/balance"
    assert req.headers["Authorization"] == "Bearer sk-test"
    assert timeout <= 5
    assert payload["balances"]["deepseek"]["is_available"] is True
    assert payload["balances"]["deepseek"]["balance_infos"][0]["total_balance"] == "12.34"


def test_deepseek_provider_config_can_override_auto_model_and_thinking(tmp_path):
    from codepilot.ai_support.providers import resolve_api_provider

    (tmp_path / "AGENTS.toml").write_text(
        """
[providers.deepseek]
api_key = "sk-config"
auto_model_selection = false
simple_model = "configured-simple-model"
complex_model = "configured-complex-model"
thinking = "enabled"
reasoning_effort = "max"
max_tokens = 1234
temperature = 0.2
""".strip(),
        encoding="utf-8",
    )

    provider = resolve_api_provider("deepseek", tmp_path)

    assert provider.api_key == "sk-config"
    assert provider.auto_model_selection is False
    assert provider.simple_model == "configured-simple-model"
    assert provider.complex_model == "configured-complex-model"
    assert provider.thinking == "enabled"
    assert provider.reasoning_effort == "max"
    assert provider.max_tokens == 1234
    assert provider.temperature == 0.2
