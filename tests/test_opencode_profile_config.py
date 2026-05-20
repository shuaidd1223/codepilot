from __future__ import annotations

import json
from pathlib import Path

from codepilot.core.config import AgentsConfig, build_provider_env_vars
from codepilot.mcp.launchers import build_mcp_launch_plan
from codepilot.opencode.env import build_opencode_config_from_agents_config
from codepilot.opencode.profile import build_opencode_profile


def test_opencode_project_tables_do_not_define_tool_profile():
    cfg = AgentsConfig.from_dict(
        {
            "opencode": {
                "profile": {
                    "brand_name": "AcmePilot",
                    "default_agent": "acme",
                    "theme": "system",
                    "scroll_speed": 5,
                    "mouse": False,
                },
                "agent": {
                    "name": "acme",
                    "description": "Acme delivery agent",
                    "prompt": "Use Acme tools first.",
                },
                "commands": {
                    "task-status": {
                        "description": "Show task status",
                        "template": "List current CodePilot tasks.",
                    }
                },
            }
        }
    )

    assert not hasattr(cfg, "opencode")


def test_opencode_project_permission_can_enable_full_access(tmp_path: Path):
    cfg = AgentsConfig.from_dict(
        {
            "opencode": {
                "profile": {"brand_name": "ProjectBrand"},
                "permission": {"mode": "full_access"},
            }
        }
    )

    opencode_cfg = build_opencode_config_from_agents_config(cfg)
    profile = build_opencode_profile(
        opencode_cfg,
        mcp_servers={},
        base_path=tmp_path / "tool-runtime" / "opencode",
    )
    payload = json.loads(profile.files[profile.env["OPENCODE_CONFIG"]])

    assert payload["permission"] == "allow"
    assert "ProjectBrand" not in json.dumps(payload)


def test_opencode_project_permission_can_use_custom_rules(tmp_path: Path):
    cfg = AgentsConfig.from_dict(
        {
            "opencode": {
                "permission": {
                    "mode": "custom",
                    "bash": "allow",
                    "edit": "ask",
                    "write": "deny",
                    "rules": {
                        "*": "ask",
                        "webfetch": "allow",
                    },
                }
            }
        }
    )

    opencode_cfg = build_opencode_config_from_agents_config(cfg)
    profile = build_opencode_profile(
        opencode_cfg,
        mcp_servers={},
        base_path=tmp_path / "tool-runtime" / "opencode",
    )
    payload = json.loads(profile.files[profile.env["OPENCODE_CONFIG"]])

    assert payload["permission"] == {
        "*": "ask",
        "bash": "allow",
        "edit": "ask",
        "write": "deny",
        "webfetch": "allow",
    }


def test_opencode_project_permission_supports_granular_custom_rules(tmp_path: Path):
    cfg = AgentsConfig.from_dict(
        {
            "opencode": {
                "permission": {
                    "mode": "custom",
                    "rules": {
                        "bash": {
                            "*": "ask",
                            "git status*": "allow",
                        },
                        "edit": {
                            "src/**": "allow",
                            ".env*": "deny",
                        },
                    },
                }
            }
        }
    )

    opencode_cfg = build_opencode_config_from_agents_config(cfg)
    profile = build_opencode_profile(
        opencode_cfg,
        mcp_servers={},
        base_path=tmp_path / "tool-runtime" / "opencode",
    )
    payload = json.loads(profile.files[profile.env["OPENCODE_CONFIG"]])

    assert payload["permission"]["bash"] == {
        "*": "ask",
        "git status*": "allow",
    }
    assert payload["permission"]["edit"] == {
        "src/**": "allow",
        ".env*": "deny",
    }


def test_opencode_profile_generates_runtime_config_files(tmp_path: Path):
    profile = build_opencode_profile(
        None,
        mcp_servers={
            "codepilot": {
                "command": "python",
                "args": ["-m", "codepilot", "mcp", "serve"],
            }
        },
        base_path=tmp_path / "tool-runtime" / "opencode",
    )

    assert profile.env == {
        "OPENCODE_CONFIG": str(tmp_path / "tool-runtime" / "opencode" / "opencode.json"),
        "OPENCODE_TUI_CONFIG": str(tmp_path / "tool-runtime" / "opencode" / "tui.json"),
        "OPENCODE_CONFIG_DIR": str(tmp_path / "tool-runtime" / "opencode" / "config"),
        "XDG_DATA_HOME": str(tmp_path / "tool-runtime" / "opencode" / "xdg-data"),
        "XDG_CACHE_HOME": str(tmp_path / "tool-runtime" / "opencode" / "xdg-cache"),
        "XDG_STATE_HOME": str(tmp_path / "tool-runtime" / "opencode" / "xdg-state"),
        "OPENCODE_DISABLE_TERMINAL_TITLE": "1",
        "CODEPILOT_OPENCODE_BRAND_NAME": "CodePilot",
    }
    assert set(Path(path).name for path in profile.files) >= {
        "opencode.json",
        "tui.json",
        "codepilot.md",
        "codepilot.en.md",
    }

    opencode_json = json.loads(profile.files[profile.env["OPENCODE_CONFIG"]])
    assert opencode_json["default_agent"] == "codepilot"
    assert opencode_json["mcp"]["codepilot"]["command"] == ["python", "-m", "codepilot", "mcp", "serve"]
    # instructions 使用绝对路径（OpenCode 源码解析：相对路径走 globUp 从 CWD 向上搜不到 config 目录的文件）
    assert len(opencode_json["instructions"]) == 1
    inst_path = opencode_json["instructions"][0]
    assert inst_path.endswith("codepilot.en.md")
    assert Path(inst_path).is_absolute()
    assert opencode_json["permission"]["bash"] == "ask"
    assert opencode_json["permission"]["edit"] == "ask"
    assert opencode_json["tools"]["bash"] is True
    assert "Task Status" in opencode_json["command"]
    assert opencode_json["command"]["Task Status"]["agent"] == "codepilot"

    tui_json = json.loads(profile.files[profile.env["OPENCODE_TUI_CONFIG"]])
    assert tui_json == {
        "$schema": "https://opencode.ai/tui.json",
        "theme": "system",
        "scroll_speed": 3,
        "diff_style": "auto",
        "mouse": True,
        "keybinds": {
            "app_exit": "ctrl+d,<leader>q",
            "input_clear": "ctrl+u",
            "input_paste": "ctrl+v,shift+insert",
        },
        "plugin": ["./config/tui-plugins/codepilot-brand.tsx"],
    }

    agent_doc = profile.files[str(tmp_path / "tool-runtime" / "opencode" / "config" / "agents" / "codepilot.md")]
    assert "CodePilot" in agent_doc
    assert "Prefer CodePilot MCP tools" in agent_doc
    # agent prompt 不再嵌入语言指令（通过 instructions 配置文件维护）
    assert "Language and Display Rules" not in agent_doc
    assert "简体中文" not in agent_doc

    instructions_doc = profile.files[
        str(tmp_path / "tool-runtime" / "opencode" / "config" / "instructions" / "codepilot.en.md")
    ]
    assert "English Interaction Rules" in instructions_doc
    # 语言指令全部在 instructions 配置文件中，使用 Markdown 二级标题
    assert "## General" in instructions_doc
    assert "## Thinking / Reasoning" in instructions_doc
    assert "All interactive output must be written in English" in instructions_doc
    command_doc = profile.files[str(tmp_path / "tool-runtime" / "opencode" / "config" / "commands" / "Task Status.md")]
    assert "List current CodePilot tasks" in command_doc
    assert "in English" in command_doc
    brand_plugin = profile.files[
        str(tmp_path / "tool-runtime" / "opencode" / "config" / "tui-plugins" / "codepilot-brand.tsx")
    ]
    assert 'const brandName = "CodePilot"' in brand_plugin
    assert "const logoLines =" in brand_plugin
    assert "_____ ____  _____  ______ _____ _____ _      ____ _______" in brand_plugin
    assert "| |   | |  | | |  | | |__  | |__) || | | |" in brand_plugin
    assert "| |___| |__| | |__| | |____| |    _| |_| |___" in brand_plugin
    assert "██████╗" not in brand_plugin
    assert "██╔════╝" not in brand_plugin
    assert "home_logo" in brand_plugin
    assert "sidebar_footer" in brand_plugin
    package_json = json.loads(profile.files[str(tmp_path / "tool-runtime" / "opencode" / "config" / "package.json")])
    assert package_json["dependencies"]["@opentui/solid"]
    assert package_json["dependencies"]["solid-js"]


def test_opencode_profile_can_generate_chinese_runtime_config(tmp_path: Path):
    cfg = AgentsConfig.from_dict({"automation": {"agent_language": "zh-CN"}})
    opencode_cfg = build_opencode_config_from_agents_config(cfg)

    profile = build_opencode_profile(
        opencode_cfg,
        mcp_servers={},
        base_path=tmp_path / "tool-runtime" / "opencode",
    )

    payload = json.loads(profile.files[profile.env["OPENCODE_CONFIG"]])
    inst_path = payload["instructions"][0]
    assert inst_path.endswith("codepilot.zh-CN.md")
    assert "任务状态" in payload["command"]
    instructions_doc = profile.files[
        str(tmp_path / "tool-runtime" / "opencode" / "config" / "instructions" / "codepilot.zh-CN.md")
    ]
    assert "中文交互规则" in instructions_doc
    assert "所有交互输出必须使用简体中文" in instructions_doc
    command_doc = profile.files[str(tmp_path / "tool-runtime" / "opencode" / "config" / "commands" / "任务状态.md")]
    assert "列出当前 CodePilot 任务" in command_doc
    assert "简体中文" in command_doc


def test_opencode_profile_enables_mouse_for_permission_dialog_clicks(tmp_path: Path):
    profile = build_opencode_profile(
        None,
        mcp_servers={},
        base_path=tmp_path / "tool-runtime" / "opencode",
    )

    tui_json = json.loads(profile.files[profile.env["OPENCODE_TUI_CONFIG"]])

    assert tui_json["mouse"] is True


def test_opencode_launcher_injects_profile_paths_and_commands(tmp_path: Path):
    plan = build_mcp_launch_plan(
        "opencode",
        executable="opencode-bin",
        mcp_servers={"codepilot": {"command": "python", "args": ["-m", "codepilot"]}},
        config_path=tmp_path / "opencode.json",
    )

    assert plan.command == ["opencode-bin", "--agent", "codepilot"]
    assert plan.env == {
        "OPENCODE_CONFIG": str(tmp_path / "opencode.json"),
        "OPENCODE_TUI_CONFIG": str(tmp_path / "tui.json"),
        "OPENCODE_CONFIG_DIR": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "OPENCODE_DISABLE_TERMINAL_TITLE": "1",
        "CODEPILOT_OPENCODE_BRAND_NAME": "CodePilot",
    }
    assert str(tmp_path / "config" / "agents" / "codepilot.md") in plan.config_files
    payload = json.loads(plan.config_files[str(tmp_path / "opencode.json")])
    assert payload["default_agent"] == "codepilot"
    assert "Prefer CodePilot MCP tools" in payload["agent"]["codepilot"]["prompt"]
    # agent prompt 不再嵌入语言指令，由 instructions 配置文件提供
    assert "简体中文" not in payload["agent"]["codepilot"]["prompt"]


def test_opencode_profile_uses_configured_openai_model_and_base_url(tmp_path: Path):
    cfg = AgentsConfig.from_dict(
        {
            "providers": {
                "openai": {
                    "enabled": True,
                    "api_key": "sk-test",
                    "base_url": "https://sub.hdd.sb/v1",
                    "model": "gpt-5.4",
                }
            }
        }
    )

    opencode_cfg = build_opencode_config_from_agents_config(cfg)
    profile = build_opencode_profile(
        opencode_cfg,
        mcp_servers={},
        base_path=tmp_path / "tool-runtime" / "opencode",
    )
    payload = json.loads(profile.files[profile.env["OPENCODE_CONFIG"]])

    assert payload["model"] == "openai/gpt-5.4"
    assert payload["agent"]["codepilot"]["model"] == "openai/gpt-5.4"
    assert "enabled_providers" not in payload
    assert payload["provider"]["openai"]["options"]["baseURL"] == "https://sub.hdd.sb/v1"
    assert payload["provider"]["openai"]["models"]["gpt-5.4"]["name"] == "GPT-5.4"


def test_opencode_profile_uses_deepseek_custom_provider(tmp_path: Path):
    cfg = AgentsConfig.from_dict(
        {
            "providers": {
                "deepseek": {
                    "enabled": True,
                    "api_key": "sk-ds-test",
                    "base_url": "https://api.deepseek.com",
                    "simple_model": "deepseek-v4-flash",
                    "complex_model": "deepseek-v4-pro",
                    "thinking": "auto",
                    "reasoning_effort": "auto",
                }
            }
        }
    )

    opencode_cfg = build_opencode_config_from_agents_config(cfg)
    profile = build_opencode_profile(
        opencode_cfg,
        mcp_servers={},
        base_path=tmp_path / "tool-runtime" / "opencode",
    )
    payload = json.loads(profile.files[profile.env["OPENCODE_CONFIG"]])

    assert payload["model"] == "deepseek/deepseek-v4-pro"
    assert payload["small_model"] == "deepseek/deepseek-v4-flash"
    assert payload["agent"]["codepilot"]["model"] == "deepseek/deepseek-v4-pro"
    assert "enabled_providers" not in payload
    provider = payload["provider"]["deepseek"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["name"] == "DeepSeek"
    assert provider["options"]["baseURL"] == "https://api.deepseek.com"
    assert provider["options"]["apiKey"] == "{env:DEEPSEEK_API_KEY}"
    assert provider["models"]["deepseek-v4-pro"]["name"] == "DeepSeek-V4-Pro"
    assert provider["models"]["deepseek-v4-pro"]["limit"]["context"] == 1048576
    assert provider["models"]["deepseek-v4-pro"]["options"]["reasoningEffort"] == "max"


def test_opencode_profile_adds_all_configured_custom_providers(tmp_path: Path):
    cfg = AgentsConfig.from_dict(
        {
            "providers": {
                "openai": {
                    "enabled": True,
                    "api_key": "sk-openai",
                    "base_url": "https://proxy.example/v1",
                    "model": "gpt-5.4",
                },
                "qwen": {
                    "enabled": True,
                    "api_key": "sk-qwen",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "model": "qwen-plus",
                },
                "hunyuan": {
                    "enabled": True,
                    "api_key": "sk-hunyuan",
                    "base_url": "https://hunyuan.cloud.tencent.com/v1",
                    "model": "hunyuan-turbo",
                },
            }
        }
    )

    opencode_cfg = build_opencode_config_from_agents_config(cfg)
    profile = build_opencode_profile(
        opencode_cfg,
        mcp_servers={},
        base_path=tmp_path / "tool-runtime" / "opencode",
    )
    payload = json.loads(profile.files[profile.env["OPENCODE_CONFIG"]])

    assert "enabled_providers" not in payload
    assert set(payload["provider"]) == {"openai", "qwen", "hunyuan"}
    assert payload["model"] == "openai/gpt-5.4"
    assert payload["provider"]["qwen"]["npm"] == "@ai-sdk/openai-compatible"
    assert payload["provider"]["qwen"]["name"] == "阿里通义千问"
    assert payload["provider"]["qwen"]["options"]["apiKey"] == "{env:DASHSCOPE_API_KEY}"
    assert payload["provider"]["qwen"]["models"]["qwen-plus"]["name"] == "QWEN-PLUS"
    assert payload["provider"]["hunyuan"]["options"]["apiKey"] == "{env:HUNYUAN_API_KEY}"
    assert payload["provider"]["hunyuan"]["models"]["hunyuan-turbo"]["name"] == "HUNYUAN-TURBO"


def test_build_provider_env_vars_returns_empty_for_no_providers():
    cfg = AgentsConfig()
    result = build_provider_env_vars(cfg)
    assert result == {}


def test_build_provider_env_vars_skips_disabled_providers():
    cfg = AgentsConfig.from_dict({
        "providers": {
            "openai-gpt4o": {
                "enabled": False,
                "api_key": "sk-test-disabled",
                "base_url": "",
            },
        },
    })
    result = build_provider_env_vars(cfg)
    assert result == {}


def test_build_provider_env_vars_injects_openai_api_key():
    cfg = AgentsConfig.from_dict({
        "providers": {
            "openai-gpt4o": {
                "enabled": True,
                "api_key": "sk-test-123",
                "base_url": "https://proxy.example/v1",
            },
        },
    })
    result = build_provider_env_vars(cfg)
    assert result == {
        "OPENAI_API_KEY": "sk-test-123",
        "OPENAI_BASE_URL": "https://proxy.example/v1",
    }


def test_build_provider_env_vars_injects_anthropic_api_key():
    cfg = AgentsConfig.from_dict({
        "providers": {
            "claude-sonnet": {
                "enabled": True,
                "api_key": "sk-ant-test",
            },
        },
    })
    result = build_provider_env_vars(cfg)
    assert result == {"ANTHROPIC_API_KEY": "sk-ant-test"}


def test_build_provider_env_vars_skips_unknown_provider():
    cfg = AgentsConfig.from_dict({
        "providers": {
            "custom-provider": {
                "enabled": True,
                "api_key": "sk-custom",
            },
        },
    })
    result = build_provider_env_vars(cfg)
    assert result == {}


def test_build_provider_env_vars_prefers_first_enabled_provider():
    cfg = AgentsConfig.from_dict({
        "providers": {
            "openai-gpt4o": {
                "enabled": True,
                "api_key": "sk-openai",
                "base_url": "https://openai.proxy/v1",
            },
            "claude-sonnet": {
                "enabled": True,
                "api_key": "sk-claude",
            },
        },
    })
    result = build_provider_env_vars(cfg)
    # Both should be injected; OpenCode will pick its preferred backend
    assert result["OPENAI_API_KEY"] == "sk-openai"
    assert result["ANTHROPIC_API_KEY"] == "sk-claude"
    assert result["OPENAI_BASE_URL"] == "https://openai.proxy/v1"
