"""LLM-prompt assembly for one address turn.

The agent loop only ever needs one thing from this module: a
:class:`PromptBuild` for the current session — the rendered message list
plus the index up to which the cacheable prefix is supposed to be
byte-stable across turns. Everything else (system-prompt rendering,
media-block prepending, render options) is private detail and lives here
so :class:`AgentLoop` stays focused on orchestration.
"""

from __future__ import annotations

import platform
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, PackageLoader

from benchclaw.agent.skills import SkillsLoader
from benchclaw.bus import MessageAddress
from benchclaw.config import AgentConfig
from benchclaw.media import MediaRepository
from benchclaw.session import RenderOptions, Session

if TYPE_CHECKING:
    from benchclaw.agent.tools.base import Tool
    from benchclaw.agent.tools.registry import ToolRegistry

BOOTSTRAP_FILES = ["AGENTS.md"]


def _xml_text(value: Any) -> str:
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_attr(value: Any) -> str:
    return _xml_text(value).replace('"', "&quot;").replace("'", "&apos;")


_jinja_env: Environment | None = None


def _env() -> Environment:
    global _jinja_env
    if _jinja_env is None:
        env = Environment(
            loader=PackageLoader("benchclaw.agent", "templates"),
            keep_trailing_newline=True,
        )
        env.filters["xml_text"] = _xml_text
        env.filters["xml_attr"] = _xml_attr
        _jinja_env = env
    return _jinja_env


def build_system_prompt(
    workspace: Path,
    *,
    tools: Iterable["Tool"] | None = None,
    channel: str | None = None,
    chat_id: str | None = None,
    session_label: str | None = None,
    model: str | None = None,
    context_window: int | None = None,
) -> str:
    bootstrap_files = [
        {"name": f, "content": (workspace / f).read_text(encoding="utf-8")}
        for f in BOOTSTRAP_FILES
        if (workspace / f).exists()
    ]
    skills_loader = SkillsLoader(workspace)
    all_skills = skills_loader.get_all_skills()
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_files = sorted(p.name for p in memory_dir.iterdir() if p.is_file())
    system = platform.system()
    return (
        _env()
        .get_template("system_prompt.j2")
        .render(
            now="",  # Tail-injected per turn (Phase 3c); kept for template compatibility.
            runtime=(
                f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, "
                f"Python {platform.python_version()}"
            ),
            workspace_path=str(workspace.expanduser().resolve()),
            bootstrap_files=bootstrap_files,
            memory_files=memory_files,
            skills=all_skills,
            tools=[
                {"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in (tools or [])
            ],
            channel=channel,
            chat_id=chat_id,
            session_label=session_label,
            model=model,
            context_window=context_window,
        )
    )


@dataclass(frozen=True)
class PromptBuild:
    """One turn's worth of LLM input.

    ``stable_prefix_end`` is the exclusive index up to which the prefix
    should be cache-stable across turns: everything before the synthetic
    tail injection (Phase 3c) or before the latest user message when
    nothing was injected. The :mod:`benchclaw.agent.cache_monitor`
    watchdog reads this index to fingerprint the cacheable prefix.
    """

    messages: list[dict[str, object]]
    stable_prefix_end: int


def _last_user_message_index(messages: list[dict[str, object]]) -> int | None:
    return next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
        None,
    )


def _prepend_media_to_last_user(
    messages: list[dict[str, object]],
    media_blocks: list[dict[str, object]] | None,
) -> list[dict[str, object]]:
    """Prepend ``media_blocks`` to the latest user message's content.

    Promotes plain-text content into a content-block list when needed.
    Returns a new list — the caller's input is not mutated.
    """
    if not media_blocks:
        return list(messages)
    last_user_idx = _last_user_message_index(messages)
    if last_user_idx is None:
        return list(messages)
    out = list(messages)
    user_msg = dict(out[last_user_idx])
    existing = user_msg.get("content", "")
    if isinstance(existing, list):
        user_msg["content"] = [*media_blocks, *existing]
    else:
        user_msg["content"] = [*media_blocks, {"type": "text", "text": existing}]
    out[last_user_idx] = user_msg
    return out


class PromptBuilder:
    """Assembles the per-turn prompt from session + workspace state.

    Held by :class:`AgentLoop` for the lifetime of the process. ``build``
    is called every turn.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        tools: "ToolRegistry",
        media_repo: MediaRepository,
        agent_config: AgentConfig,
    ) -> None:
        self.workspace = workspace
        self.tools = tools
        self.media_repo = media_repo
        self.agent_config = agent_config

    def render_options(self) -> RenderOptions:
        return RenderOptions()

    def build(
        self,
        session: Session,
        addr: MessageAddress,
        pending_media: list[str] | None = None,
    ) -> PromptBuild:
        system_prompt = build_system_prompt(
            self.workspace,
            tools=self.tools.values(),
            channel=addr.channel,
            chat_id=addr.chat_id,
            session_label=session.describe_current_session(),
            model=self.agent_config.model,
            context_window=self.agent_config.context_window,
        )
        messages = session.render_llm_messages(
            system_prompt,
            self.media_repo,
            self.render_options(),
            max_messages=self.agent_config.memory_window,
        )
        media_blocks: list[dict[str, object]] | None = None
        if pending_media and self.media_repo:
            media_blocks = self.media_repo.build_media_blocks(pending_media)
        out = _prepend_media_to_last_user(messages, media_blocks)
        last_user_idx = _last_user_message_index(out)
        stable_prefix_end = last_user_idx if last_user_idx is not None else len(out)
        return PromptBuild(messages=out, stable_prefix_end=stable_prefix_end)
