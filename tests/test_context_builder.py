from __future__ import annotations

from pathlib import Path
from typing import Any

from benchclaw.agent.prompt import build_system_prompt


class _DummyTool:
    def __init__(self, name: str, description: str, parameters: dict[str, Any]) -> None:
        self._name = name
        self._description = description
        self._parameters = parameters

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters


def test_build_system_prompt_xml_escapes_session_label(tmp_path: Path) -> None:
    """Session labels are user-controllable via channel metadata, so they
    have to land in the prompt entity-escaped.

    Phase 5c removed the inline ``<tools>`` listing from the system
    prompt — duplication with the OpenAI ``tools=`` parameter confused
    Gemma into mixing call formats — so this test no longer asserts on
    rendered tool tags."""
    prompt = build_system_prompt(
        tmp_path,
        channel="whatsapp",
        chat_id="123&456",
        session_label='Alice "A" & Bob',
    )

    assert 'Session: Alice "A" &amp; Bob' in prompt
    assert "TODO:" not in prompt


def test_build_system_prompt_describes_media_annotation_flow(tmp_path: Path) -> None:
    tool = _DummyTool(
        name="annotate_media",
        description="Save image annotations.",
        parameters={"type": "object", "properties": {}, "required": []},
    )

    prompt = build_system_prompt(tmp_path, tools=[tool])

    assert "<private_tags>" not in prompt
    assert "annotate_media" in prompt
    assert "MUST call `annotate_media`" in prompt
