"""Built-in prompt templates for the local Prompt Sidecar editor."""

from __future__ import annotations


BUILTIN_MODE_TEMPLATES: dict[str, str] = {
    "quick": (
        "Keep the result concise and directly copy-ready. "
        "This mode only rewrites the request."
    ),
    "structured": (
        "Use clear sections: Goal, Context, Task, Constraints, Output format, and "
        "Acceptance criteria. This mode only rewrites the request; omit sections that "
        "would only add empty boilerplate."
    ),
    "agent": (
        "Write a directly executable Codex coding-agent task prompt. Include the fixed "
        "Codex execution protocol below, then organize the prompt as task goal, current "
        "context, execution protocol, subtask and multi-session collaboration requirements, "
        "completion criteria, verification, human decision gates, and output requirements. "
        "Preserve the user's scope and include a rule not to commit or push unless explicitly "
        "requested."
    ),
    "system": (
        "Write a reusable system prompt. This mode only rewrites the requested instruction. "
        "Include role, responsibilities, boundaries, tool use, uncertainty handling, and "
        "output habits."
    ),
}

BUILTIN_STYLE_TEMPLATES: dict[str, str] = {
    "concise": (
        "First line must give the conclusion. Use at most 1-3 bullets per section. "
        "Omit empty sections. Do not repeat the request or add generic closings."
    ),
    "standard": (
        "Give the conclusion first, then use the requested sections with supporting evidence. "
        "Use at most 3-5 bullets per section and omit empty sections."
    ),
    "detailed": (
        "Give the conclusion first, then use the requested sections with evidence and risks. "
        "Keep detail useful; do not restate the request or duplicate facts."
    ),
}
