"""Small registry and schema helpers for CodePilot MCP tools."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import NoneType, UnionType
from typing import Any, Callable, Mapping, Union, get_args, get_origin, get_type_hints


JsonSchema = dict[str, Any]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    func: Callable[..., Any]
    schema: JsonSchema


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        func: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Callable[..., Any]:
        tool_name = name or func.__name__
        if tool_name in self._tools:
            raise ValueError(f"MCP tool already registered: {tool_name}")
        schema = build_tool_schema(func, name=tool_name, description=description)
        self._tools[tool_name] = ToolDefinition(name=tool_name, func=func, schema=schema)
        return func

    def get(self, name: str) -> ToolDefinition:
        return self._tools[name]

    def list(self) -> list[ToolDefinition]:
        return list(self._tools.values())


default_registry = ToolRegistry()


def register_tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    registry: ToolRegistry | None = None,
) -> Callable[..., Any]:
    target_registry = registry or default_registry

    def decorator(target: Callable[..., Any]) -> Callable[..., Any]:
        return target_registry.register(target, name=name, description=description)

    if func is None:
        return decorator
    return decorator(func)


def build_tool_schema(
    func: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
) -> JsonSchema:
    signature = inspect.signature(func)
    hints = get_type_hints(func)
    properties: dict[str, JsonSchema] = {}
    required: list[str] = []

    for param_name, parameter in signature.parameters.items():
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise TypeError(f"unsupported parameter kind for {param_name}")
        if param_name not in hints:
            raise TypeError(f"MCP tool parameter {param_name} is missing a type annotation")

        param_schema = _annotation_to_schema(hints[param_name], context=param_name)
        if parameter.default is inspect.Parameter.empty:
            required.append(param_name)
        else:
            param_schema = dict(param_schema)
            param_schema["default"] = parameter.default
        properties[param_name] = param_schema

    if "return" not in hints:
        raise TypeError(f"MCP tool {func.__name__} is missing a return type annotation")

    doc = inspect.getdoc(func) or ""
    return {
        "name": name or func.__name__,
        "description": description if description is not None else doc,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "outputSchema": _annotation_to_schema(hints["return"], context="return"),
    }


def _annotation_to_schema(annotation: Any, *, context: str) -> JsonSchema:
    if annotation is Any:
        return {}
    if annotation is None or annotation is NoneType:
        return {"type": "null"}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation in (dict, Mapping):
        return {"type": "object"}
    if annotation is list:
        return {"type": "array"}

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in (Union, UnionType):
        non_null = [arg for arg in args if arg is not NoneType]
        if len(non_null) == 1 and len(non_null) != len(args):
            schema = _annotation_to_schema(non_null[0], context=context)
            return {"anyOf": [schema, {"type": "null"}]}
        return {"anyOf": [_annotation_to_schema(arg, context=context) for arg in args]}

    if origin is list:
        if len(args) != 1:
            raise TypeError(f"unsupported annotation for {context}: {annotation!r}")
        return {"type": "array", "items": _annotation_to_schema(args[0], context=context)}

    if origin is dict:
        if len(args) != 2 or args[0] is not str:
            raise TypeError(f"unsupported annotation for {context}: {annotation!r}")
        return {
            "type": "object",
            "additionalProperties": _annotation_to_schema(args[1], context=context),
        }

    raise TypeError(f"unsupported annotation for {context}: {annotation!r}")
