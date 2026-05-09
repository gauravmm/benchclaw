"""Proactive compaction for long-running sessions.

When the most recent LLM response shows the prompt has crossed the
configured budget threshold, the session compacts to a single
``SummaryEvent``. The compactor itself does not call the LLM — that's
the simple, deterministic story BenchClaw ships today.
"""

from __future__ import annotations

from loguru import logger

from benchclaw.bus import MessageAddress
from benchclaw.config import AgentConfig
from benchclaw.session import Session

# Compact when total_tokens (from the most recent LLM usage report) crosses
# this fraction of the configured context window. Same value the loop has
# used since BenchClaw's first commits — pulled out so a future config
# knob can override it.
COMPACT_THRESHOLD: float = 0.8


class Compactor:
    """Decides when to compact and runs the compaction call."""

    def __init__(self, agent_config: AgentConfig) -> None:
        self.config = agent_config

    def maybe_compact(
        self,
        session: Session,
        addr: MessageAddress,
        total_tokens: int,
    ) -> bool:
        """Trigger compaction when the budget threshold is exceeded.

        Returns True if compaction ran. ``total_tokens`` is the LLM's own
        prompt+completion accounting for the turn that just finished.
        """
        if total_tokens <= self.config.context_window * COMPACT_THRESHOLD:
            return False
        logger.warning(
            f"Compacting session {addr}: {total_tokens}/{self.config.context_window} tokens"
        )
        session.compact()
        logger.warning(
            f"Session {addr} compacted: {len(session.events)} events remain, "
            f"compacted_through={session.compacted_through}"
        )
        return True
