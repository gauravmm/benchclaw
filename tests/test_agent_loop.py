from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from benchclaw.agent.loop import AgentLoop, ToolCallTracker
from benchclaw.agent.tools.base import ToolContext
from benchclaw.bus import MessageAddress, MessageBus, OutboundMessage
from benchclaw.config import Config
from benchclaw.media import MediaRepository
from benchclaw.providers.base import LLMProvider, LLMResponse
from benchclaw.session import Session

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_1X1)


class _FakeProvider(LLMProvider):
    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        return self._response


def _make_loop(tmp_path: Path, response: LLMResponse) -> AgentLoop:
    config = Config()
    config.agents.master.workspace = str(tmp_path)
    return AgentLoop(config=config, bus=MessageBus(), provider=_FakeProvider(response))


def test_extract_inner_tags_returns_visible_content_and_structured_tags(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path=tmp_path, response=LLMResponse(content=""))
    visible_content, inner = loop._extract_inner_tags(
        "\n".join(
            [
                '<image_caption path="media/x.png">receipt total $12.34</image_caption>',
                "<plan>1. Check shipping</plan>",
                "Status update for the user.",
                "<log>Fetched tracking page.</log>",
                "<log>ETA is 2026-03-17.</log>",
            ]
        )
    )

    assert visible_content == "Status update for the user."
    assert [tag.body for tag in inner.tags["plan"]] == ["1. Check shipping"]
    assert inner.tags["image_caption"][0].attrs == {"path": "media/x.png"}
    assert inner.tags["image_caption"][0].body == "receipt total $12.34"
    assert "log" in inner.tags
    assert [tag.body for tag in inner.tags["log"]] == [
        "Fetched tracking page.",
        "ETA is 2026-03-17.",
    ]


@pytest.mark.asyncio
async def test_process_llm_turn_appends_inline_log_tags(tmp_path: Path) -> None:
    loop = _make_loop(
        tmp_path,
        LLMResponse(
            content=(
                "<log>Fetched order status: shipped.</log>\n"
                "Status update for you.\n"
                "<log>ETA is 2026-03-17.</log>"
            )
        ),
    )
    addr = MessageAddress("telegram", "123")
    session = Session(addr)
    session.add_message("user", "What is the order status?")
    tracker = ToolCallTracker(loop.context)

    async with loop.tools:
        call_ctx = ToolContext(
            workspace=loop.tools._master_ctx.workspace,
            bus=loop.bus,
            log_store=loop.tools._master_ctx.log_store,
            media_repo=loop.media_repo,
            address=addr,
            background_tasks=tracker.tasks,
        )
        new_plan, elapsed = await loop._process_llm_turn(
            session=session,
            tracker=tracker,
            call_ctx=call_ctx,
            addr=addr,
            current_plan=None,
        )
        outbound = await loop.bus.consume_outbound(channel="telegram")
        log_text = loop.tools._master_ctx.log_store.read_recent(n=10)

    assert new_plan is None
    assert elapsed >= 0
    assert isinstance(outbound, OutboundMessage)
    assert outbound.content == "Status update for you."
    assert session.messages[-1]["content"] == "Status update for you."
    assert "Fetched order status: shipped." in log_text
    assert "ETA is 2026-03-17." in log_text


@pytest.mark.asyncio
async def test_process_llm_turn_handles_plan_and_image_caption_via_same_inner_tag_loop(
    tmp_path: Path,
) -> None:
    image = tmp_path / "media" / "x.png"
    _write_png(image)
    repo = MediaRepository(tmp_path)
    repo.load()
    loop = _make_loop(
        tmp_path,
        LLMResponse(
            content=(
                '<image_caption path="media/x.png">receipt total $12.34</image_caption>\n'
                "<plan>1. Check shipping</plan>\n"
                "Status update for you."
            )
        ),
    )
    loop.media_repo = repo
    loop.tools._master_ctx.media_repo = repo
    addr = MessageAddress("telegram", "123")
    session = Session(addr)
    session.add_message("user", "What is in the image?")
    tracker = ToolCallTracker(loop.context)

    async with loop.tools:
        call_ctx = ToolContext(
            workspace=loop.tools._master_ctx.workspace,
            bus=loop.bus,
            log_store=loop.tools._master_ctx.log_store,
            media_repo=repo,
            address=addr,
            background_tasks=tracker.tasks,
        )
        new_plan, _elapsed = await loop._process_llm_turn(
            session=session,
            tracker=tracker,
            call_ctx=call_ctx,
            addr=addr,
            current_plan=None,
            media_repo=repo,
        )

    assert new_plan == "1. Check shipping"
    records = list(repo.iter_records())
    assert records[0]["path"] == "media/x.png"
    assert records[0]["caption"] == "receipt total $12.34"
