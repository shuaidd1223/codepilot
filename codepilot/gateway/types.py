"""Types shared by AI gateway facade and internal runners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class GatewayRequest:
    prompt: str
    schema: Optional[dict] = None
    classifier_provider: str = ""
    classifier_model: str = ""
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    project_path: str = ""
    config_ref: str = ""
    planner: str = "codex"  # CLI fallback family
    timeout: int = 60


@dataclass(frozen=True)
class GatewayCallOptions:
    """Reusable fallback options shared by chat / clarify callsites."""

    classifier_provider: str = ""
    classifier_model: str = ""
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    project_path: str = ""
    config_ref: str = ""
    planner: str = "codex"
    timeout: int = 60


@dataclass
class GatewayResponse:
    ok: bool
    source: str  # "api:<key>" | "cli:claude" | "cli:codex" | "default"
    payload: Optional[dict] = None   # populated when schema was set
    text: str = ""                   # populated when schema was None
    error: str = ""                  # non-empty when ok is False


@dataclass(frozen=True)
class GatewayMode:
    """Mode-specific pieces while sharing one routing skeleton."""

    name: str
    schema_required: bool
    schema_error: str
    api_formatter: Callable[[str, str], GatewayResponse]
