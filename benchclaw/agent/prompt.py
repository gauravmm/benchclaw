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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, PackageLoader

from benchclaw.agent.skills import SkillsLoader
from benchclaw.bus import MessageAddress
from benchclaw.config import AgentConfig
from benchclaw.media import MediaRepository
from benchclaw.session import RenderOptions, Session
from benchclaw.utils import now_aware

if TYPE_CHECKING:
    from benchclaw.agent.tools.base import Tool
    from benchclaw.agent.tools.registry import ToolRegistry

# Signature for tail providers: takes the active address, returns either a
# tag/body tuple (rendered as ``<tag>body</tag>``) or None to skip this turn.
TailProvider = Callable[[MessageAddress], tuple[str, str] | None]

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
    """Render the cache-stable portion of the prompt.

    Time and other turn-local context lives in :meth:`PromptBuilder.build`'s
    tail injection, not here, so this output stays byte-identical across
    turns and the upstream prefix cache hits.
    """
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


def _insert_synthetic_tail(
    messages: list[dict[str, object]],
    blocks: list[tuple[str, str]],
) -> tuple[list[dict[str, object]], int]:
    """Insert a synthetic ``user`` message holding the tail blocks just
    before the latest user turn.

    Returns ``(messages, stable_prefix_end)``. Keeping the turn-local
    context in a separate message — rather than splicing it into the
    system prompt — is what lets the system-prompt prefix stay
    byte-identical across turns and the upstream cache hit.

    ``blocks`` are rendered as ``<tag>body</tag>``; an empty ``blocks``
    list returns ``(messages, last_user_idx)`` unchanged.
    """
    last_user_idx = _last_user_message_index(messages)
    if last_user_idx is None:
        return list(messages), len(messages)
    if not blocks:
        return list(messages), last_user_idx
    parts = [f"<{tag}>{body}</{tag}>" for tag, body in blocks]
    tail_msg: dict[str, object] = {"role": "user", "content": "\n".join(parts)}
    out = list(messages)
    out.insert(last_user_idx, tail_msg)
    return out, last_user_idx


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


def _current_time_provider(_addr: MessageAddress) -> tuple[str, str] | None:
    return ("current_time", now_aware().strftime("%Y-%m-%d %H:%M (%A) %z"))


class PromptBuilder:
    """Assembles the per-turn prompt from session + workspace state.

    Held by :class:`AgentLoop` for the lifetime of the process. ``build``
    is called every turn. Other modules can append persistent synthetic
    tail messages via :meth:`register_tail_provider` — the substrate for
    "ambient fact" features (current time is the only built-in).
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
        self._tail_providers: list[TailProvider] = [_current_time_provider]

    def register_tail_provider(self, provider: TailProvider) -> None:
        """Append *provider* to the tail-injection chain.

        Each provider is called once per ``build`` and may return a
        ``(tag, body)`` pair to be rendered as ``<tag>body</tag>`` in
        the synthetic tail message, or ``None`` to skip this turn.
        Order matches registration order.
        """
        self._tail_providers.append(provider)

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
        with_media = _prepend_media_to_last_user(messages, media_blocks)
        tail_blocks = [block for p in self._tail_providers if (block := p(addr)) is not None]
        out, stable_prefix_end = _insert_synthetic_tail(with_media, tail_blocks)
        return PromptBuild(messages=out, stable_prefix_end=stable_prefix_end)
