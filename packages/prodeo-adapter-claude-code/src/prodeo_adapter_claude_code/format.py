"""Shared human-facing formatting for Claude Code permission requests.

Both delivery paths — the SDK ``can_use_tool`` bridge and the interactive
``PermissionRequest`` hook (ADR-0011) — must present a permission identically,
so the dashboard card and the voice readout do not depend on how the session
was started.
"""

import json
from typing import Any

#: Tool input shown to the human, capped so one giant argument blob cannot
#: bloat an interaction card (the full input still reaches the agent).
INTERACTION_BODY_CHARS = 4000


def permission_prompt(tool_name: str, input_data: dict[str, Any]) -> tuple[str, str]:
    """(title, body) for a permission request on ``tool_name``."""
    body = json.dumps(input_data, indent=2, default=str)[:INTERACTION_BODY_CHARS]
    return f"Allow {tool_name}?", body
