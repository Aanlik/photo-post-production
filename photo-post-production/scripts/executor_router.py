"""Route atomic edit operations to the strongest verified execution tier."""

from __future__ import annotations

from typing import Any


_LOCAL_UTILITY = {"metadata-write", "resize", "format-convert", "rotate"}
_LIGHTROOM = {"global-tone", "white-balance", "lens-correct", "noise-reduce", "crop-and-straighten"}
_GENERATIVE = {"generative-fill", "generative-expand", "replace-background", "add-element"}


def route_operation(operation: dict[str, Any], capabilities: dict[str, Any], descriptor: dict[str, Any] | None = None) -> dict[str, Any]:
    operation_type = str(operation.get("type") or "")
    stable_tools = {str(item) for item in capabilities.get("stable_tools", [])} if isinstance(capabilities.get("stable_tools"), list) else set()
    if operation_type in _LOCAL_UTILITY:
        return {"tier": "stable-auto", "backend": "local-utility", "reason": "deterministic_local_operation"}
    if operation_type in _LIGHTROOM:
        return {"tier": "stable-auto", "backend": "lightroom-mcp", "reason": "raw_global_operation"}
    if operation_type in stable_tools:
        return {"tier": "stable-auto", "backend": "photoshop-fine-edit", "reason": "verified_structured_tool"}
    if descriptor and descriptor.get("verified") is True and descriptor.get("operation_type") == operation_type:
        return {"tier": "descriptor-verified", "backend": "photoshop-batchplay", "descriptor_id": descriptor.get("descriptor_id"), "reason": "verified_action_descriptor"}
    if operation_type in _GENERATIVE and capabilities.get("generative_available") is True:
        return {"tier": "stable-auto", "backend": "chat-window-imagegen", "reason": "host_generative_tool_available"}
    if capabilities.get("ui_available") is True:
        return {"tier": "ui-assisted", "backend": "photoshop-ui", "reason": "no_structured_or_descriptor_path", "requires_post_validation": True}
    return {"tier": "unsupported", "backend": None, "reason": "no_verified_executor"}
