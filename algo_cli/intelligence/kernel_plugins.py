"""B45. Kernel Plugin Architecture with Typed Annotations (Semantic Kernel Pattern).

Tools are plain Python methods decorated with @kernel_function and typed
with Annotated[T, "description"].  The kernel introspects the function
signature and auto-generates the tool schema — no manual JSON schema
authoring.  Structured outputs use Pydantic BaseModel with response_format.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, Callable, Union, get_args, get_origin, get_type_hints


@dataclass
class ToolParam:
    name: str
    type: str
    description: str
    required: bool


@dataclass
class ToolSchema:
    name: str
    description: str
    params: list[ToolParam] = field(default_factory=list)
    return_type: str = "string"
    return_description: str = ""


def kernel_function(description: str = "") -> Callable:
    """Decorator that marks a method as a kernel-callable tool."""
    def decorator(fn: Callable) -> Callable:
        setattr(fn, "_kernel_description", description or fn.__doc__ or "")
        setattr(fn, "_kernel_function", True)
        return fn
    return decorator


def introspect_tool(fn: Callable) -> ToolSchema:
    """Auto-generate tool schema from function signature + Annotated hints."""
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}

    params: list[ToolParam] = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        hint = hints.get(name, str)
        desc = ""
        type_name = "string"

        # Extract Annotated[T, "description"]
        if hasattr(hint, "__metadata__"):
            origin = getattr(hint, "__origin__", str)
            type_name = getattr(origin, "__name__", "string")
            if hint.__metadata__:
                desc = str(hint.__metadata__[0])
        else:
            type_name = getattr(hint, "__name__", "string")

        params.append(ToolParam(
            name=name,
            type=type_name,
            description=desc,
            required=param.default is inspect.Parameter.empty,
        ))

    # Return type
    ret_hint = hints.get("return", type(None))
    ret_desc = ""
    if hasattr(ret_hint, "__metadata__"):
        ret_desc = str(ret_hint.__metadata__[0]) if ret_hint.__metadata__ else ""
    ret_origin = getattr(ret_hint, "__origin__", ret_hint)
    ret_type = getattr(ret_origin, "__name__", "void")

    return ToolSchema(
        name=fn.__name__,
        description=getattr(fn, "_kernel_description", fn.__doc__ or ""),
        params=params,
        return_type=ret_type,
        return_description=ret_desc,
    )


class Kernel:
    """Central kernel that manages plugins and invokes tools."""

    def __init__(self) -> None:
        self._plugins: dict[str, Any] = {}
        self._schemas: dict[str, ToolSchema] = {}
        self._handlers: dict[str, Callable] = {}

    def add_plugin(self, plugin: Any, name: str | None = None) -> None:
        """Register a plugin class — all @kernel_function methods become tools."""
        plugin_name = name or plugin.__class__.__name__
        self._plugins[plugin_name] = plugin
        for attr_name in dir(plugin):
            attr = getattr(plugin, attr_name, None)
            if callable(attr) and getattr(attr, "_kernel_function", False):
                schema = introspect_tool(attr)
                self._schemas[schema.name] = schema
                self._handlers[schema.name] = attr

    def list_tools(self) -> list[ToolSchema]:
        return list(self._schemas.values())

    def get_tool_schema(self, name: str) -> ToolSchema | None:
        return self._schemas.get(name)

    def invoke(self, tool_name: str, **kwargs: Any) -> str:
        """Invoke a registered tool by name."""
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"Tool '{tool_name}' not found"
        try:
            result = handler(**kwargs)
            return str(result)
        except Exception as e:
            return f"Tool '{tool_name}' error: {e}"

    def tool_descriptions_for_prompt(self) -> str:
        """Generate tool description text for inclusion in system prompt."""
        lines: list[str] = []
        for schema in self._schemas.values():
            param_str = ", ".join(
                f"{p.name}: {p.type}" + (f" — {p.description}" if p.description else "")
                for p in schema.params
            )
            lines.append(f"- {schema.name}({param_str}): {schema.description}")
        return "\n".join(lines)


# ── structured output support ─────────────────────────────────────────


def _resolved_annotations(model_class: type) -> dict[str, Any]:
    """Annotations with string forms resolved; unresolvable ones become Any (unchecked)."""
    raw = dict(getattr(model_class, "__annotations__", {}) or {})
    resolved: dict[str, Any] = {}
    for name, value in raw.items():
        if not isinstance(value, str):
            resolved[name] = value
            continue
        # Resolve one field at a time so a single unknown name does not make
        # every other field unchecked.
        holder = type("_AnnotationHolder", (), {"__annotations__": {name: value}})
        holder.__module__ = getattr(model_class, "__module__", holder.__module__)
        try:
            resolved[name] = get_type_hints(holder, localns=dict(vars(model_class)))[name]
        except Exception:
            resolved[name] = Any
    return resolved


def _accepted_types(annotation: Any) -> tuple[type, ...] | None:
    """Concrete types an annotation accepts, or None when it cannot be checked with isinstance."""
    if annotation is Any:
        return None
    if annotation is None or annotation is type(None):
        return (type(None),)
    origin = get_origin(annotation)
    if origin is Union or origin is UnionType:
        members: list[type] = []
        for arg in get_args(annotation):
            sub = _accepted_types(arg)
            if sub is None:
                return None
            members.extend(sub)
        return tuple(members)
    target = origin or annotation
    return (target,) if isinstance(target, type) else None


def validate_structured_output(data: dict, model_class: type) -> dict:
    """Validate LLM output against a Pydantic-like BaseModel.

    Works with any class that has __annotations__ (dataclass or Pydantic).
    """
    try:
        from pydantic import BaseModel
        if issubclass(model_class, BaseModel):
            obj = model_class(**data)
            return {"valid": True, "data": obj.model_dump()}
    except ImportError:
        pass
    except Exception as e:
        return {"valid": False, "error": str(e)}

    # Fallback: simple annotation-based validation
    annotations = getattr(model_class, "__annotations__", {})
    resolved = _resolved_annotations(model_class)
    errors: list[str] = []
    for field_name, field_type in annotations.items():
        if field_name not in data:
            errors.append(f"missing field: {field_name}")
            continue
        accepted = _accepted_types(resolved.get(field_name, Any))
        if accepted is not None and not isinstance(data[field_name], accepted):
            expected = getattr(field_type, "__name__", None) or str(field_type)
            errors.append(f"{field_name}: expected {expected}, got {type(data[field_name]).__name__}")
    if errors:
        return {"valid": False, "error": "; ".join(errors)}
    return {"valid": True, "data": data}
