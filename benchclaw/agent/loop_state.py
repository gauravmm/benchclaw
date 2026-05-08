"""Per-address agent loop state and the in-flight tool tracker.

Pulled out of ``loop.py`` so the orchestration in :class:`AgentLoop`
stays focused on the runtime; everything in here is data + a small
state machine for tracking background tool execution.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from benchclaw.bus import ToolResultEvent
from benchclaw.session import Session, SystemEvent, ToolEvent


@dataclass
class AddressState:
    iteration_count: int = 0
    pending_system_events: list[str] = field(default_factory=list)
    pending_media: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BatchApplication:
    needs_llm: bool = False
    start_typing: bool = False


class ToolCallTracker:
    """Per-address tracker for in-flight background tool calls."""

    def __init__(self) -> None:
        self._in_flight: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # Set when an assistant turn dispatches exactly one tool call AND
        # that tool was declared ``terminal_when_lone``. The loop reads
        # this when the tool result drains the in-flight set: True ⇒ skip
        # the follow-up LLM call (the tool already produced the reply).
        self._terminal_lone_pending: bool = False

    @property
    def tasks(self) -> dict[str, asyncio.Task]:
        return self._tasks

    @property
    def pending(self) -> bool:
        return bool(self._in_flight)

    def add(self, tool_call_id: str, tool_name: str, task: asyncio.Task) -> None:
        self._in_flight[tool_call_id] = tool_name
        self._tasks[tool_call_id] = task

    def mark_turn_terminal_when_lone(self) -> None:
        """Signal that the assistant turn currently being dispatched ends
        when its lone tool call resolves — no follow-up LLM turn."""
        self._terminal_lone_pending = True

    def take_terminal_when_lone(self) -> bool:
        """Read-and-clear: True iff the just-completed tool batch was a
        terminal_when_lone single-call turn."""
        was = self._terminal_lone_pending
        self._terminal_lone_pending = False
        return was

    def handle_interrupt(self, session: Session) -> None:
        if not self._in_flight:
            return
        tool_list = ", ".join(f"{name} ({tid[:8]})" for tid, name in self._in_flight.items())
        session.append(
            SystemEvent(
                content="The following tools are still executing in the background: "
                f"{tool_list}. Their results will arrive as new events."
            )
        )
        self._in_flight.clear()

    def handle_result(self, event: ToolResultEvent, session: Session) -> bool:
        """Append the tool event to *session*. Returns True if this completion
        either drained the in-flight set (so the loop should run an LLM turn
        for the foreground reply) or arrived from a background tool the
        tracker no longer remembers (so the loop should still nudge the
        model)."""
        self._tasks.pop(event.tool_call_id, None)
        session.append(
            ToolEvent(
                content=event.result,
                tool_call_id=event.tool_call_id,
                tool_name=event.tool_name,
            )
        )
        if event.tool_call_id in self._in_flight:
            del self._in_flight[event.tool_call_id]
            return not self._in_flight

        session.append(
            SystemEvent(
                content=(
                    f"Background tool '{event.tool_name}' completed. Summarize the "
                    "result for the user or take any necessary follow-up actions to "
                    "achieve the goal."
                )
            )
        )
        return True
