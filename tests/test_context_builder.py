from __future__ import annotations

from pathlib import Path
from typing import Any

from benchclaw.agent.context.builder import ContextBuilder


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


def test_build_system_prompt_uses_xml_safe_rendering(tmp_path: Path) -> None:
    builder = ContextBuilder(tmp_path)
    tool = _DummyTool(
        name='quote"tool',
        description='Say "hi" & compare <values>.',
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": 'Path with "quotes" & symbols'},
            },
            "required": ["path"],
        },
    )

    prompt = builder.build_system_prompt(
        tools=[tool],
        channel="whatsapp",
        chat_id="123&456",
        session_label='Alice "A" & Bob',
    )

    assert '<tool name="quote&quot;tool">' in prompt
    assert 'Say "hi" &amp; compare &lt;values&gt;.' in prompt
    assert 'Path with "quotes" &amp; symbols' in prompt
    assert "params=" not in prompt
    assert 'Session: Alice "A" &amp; Bob' in prompt
    assert "TODO:" not in prompt
