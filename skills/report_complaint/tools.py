"""Skill-local tools for report_complaint.

Only the post-write hook lives here. Everything it needs is in the shared
`tools/civic.py`; a post-write hook has to be declared by the skill that owns
the field it fires on, which is why this file exists at all.
"""

from __future__ import annotations

from rasa.mantle.tools.decorator import ToolContext, tool
from rasa.mantle.tools.result import ToolResult

from tools.civic import _locate, _memory, _publish_location_status


@tool(description="Internal post-write hook for spoken_location.")
async def run_after_setting_spoken_location(context: ToolContext = None) -> ToolResult:
    """Resolve the location the moment the caller's words are recorded.

    Asking the model to call a location tool part-way through a step did not
    survive the move to rasa-pro 3.20: it answered the caller in prose and
    never called anything, leaving the complaint with no coordinates and no
    error to show for it. Writing a memory field is the one action the engine
    drives reliably, so the write is what the model is asked for and the search
    hangs off it here.

    Engine-invoked, never offered to the LLM, and takes no arguments — it reads
    what was just written.
    """
    spoken = _memory(context, "spoken_location") or ""
    cleaned = _memory(context, "cleaned_query") or ""
    result = await _locate(spoken, cleaned, context)
    payload = _publish_location_status(context, result.llm_response)
    status = str(payload.get("status") or "")

    # Rejecting rolls the write back, which is right for exactly one case: the
    # caller described a spot before there was any confirmed area to place it
    # in. Leaving that value sitting in memory makes the location look
    # answered, and the model moves on without ever locating anything.
    if status in {"need_area_first", "category_not_set"}:
        return ToolResult(
            llm_response={
                "ok": False,
                "error": payload.get("hint") or "Confirm the area first.",
                "status": status,
            }
        )

    # Every other outcome stands, including "the map found nothing" — that is a
    # legitimate consequence of them having spoken, not a bad write.
    return ToolResult(llm_response={"ok": True, "status": status})
