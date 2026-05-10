"""Config-level tests for the tool_reminders section."""

from __future__ import annotations

import pytest

from benchclaw.config import Config, ToolReminder


def test_bare_string_reminder_coerces_to_persistent() -> None:
    config = Config.model_validate({"tool_reminders": {"search_media": "cite when answering"}})
    assert config.tool_reminders == {
        "search_media": ToolReminder(text="cite when answering", ephemeral=False),
    }


def test_dict_form_reminder_preserves_ephemeral_flag() -> None:
    config = Config.model_validate(
        {
            "tool_reminders": {
                "cute-db__search_cute": {"text": "call send_media", "ephemeral": True},
            }
        }
    )
    assert config.tool_reminders["cute-db__search_cute"].ephemeral is True


def test_mixed_forms_in_one_section() -> None:
    config = Config.model_validate(
        {
            "tool_reminders": {
                "search_media": "cite when answering",
                "cute-db__search_cute": {"text": "call send_media", "ephemeral": True},
            }
        }
    )
    assert config.tool_reminders["search_media"].ephemeral is False
    assert config.tool_reminders["cute-db__search_cute"].ephemeral is True


def test_empty_text_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Config.model_validate({"tool_reminders": {"search_media": "   "}})


def test_default_is_empty_dict() -> None:
    assert Config().tool_reminders == {}
