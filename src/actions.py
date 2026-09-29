"""Authored native actions shared by the editor, delivery, and executor."""

from typing import Any


def normalize_action(action: Any, *, allow_incomplete: bool = False) -> dict[str, str]:
    """Validate one action; drafts may omit required content until execution."""
    if not isinstance(action, dict) or set(action) - {"tool", "target", "text", "key"}:
        raise ValueError("An action must contain only tool, target, text, and key fields.")
    tool = action.get("tool")
    if not isinstance(tool, str) or tool not in {"click", "type_text", "press_key", "read"}:
        raise ValueError("Choose click, type_text, press_key, or read.")
    normalized = {"tool": tool}
    target = action.get("target")
    if target is not None:
        if not isinstance(target, str) or len(target) > 2000:
            raise ValueError("The target must contain at most 2000 characters.")
        if target.strip():
            normalized["target"] = target.strip()
    if not allow_incomplete and tool in {"click", "read"} and "target" not in normalized:
        raise ValueError("Click and read actions need a target description.")
    for field, limit in (("text", 20000), ("key", 100)):
        value = action.get(field)
        if value is not None:
            if not isinstance(value, str) or not (0 if allow_incomplete else 1) <= len(value) <= limit:
                raise ValueError(f"{field.capitalize()} must contain 1–{limit} characters.")
            normalized[field] = value
    if not allow_incomplete and tool == "type_text" and "text" not in normalized:
        raise ValueError("Type actions need the exact text to send.")
    if not allow_incomplete and tool == "press_key" and ("key" not in normalized or not normalized["key"].strip()):
        raise ValueError("Press key actions need a key such as Enter or Tab.")
    if "text" in normalized and tool != "type_text":
        raise ValueError("Only type_text actions accept text.")
    if "key" in normalized and tool != "press_key":
        raise ValueError("Only press_key actions accept a key.")
    return normalized


def action_instruction(action: dict[str, str]) -> str:
    """Describe the authored action without sending literal typing data to Jev."""
    target = action.get("target", "the currently focused control")
    return {
        "click": f"Click {target}",
        "type_text": f"Type the supplied text into {target}",
        "press_key": f"Press {action.get('key', '')} in {target}",
        "read": f"Read the text of {target}",
    }[action["tool"]]
