from datetime import datetime, timedelta

from benchclaw.bus import MessageBus
from benchclaw.channels.base import BaseChannel, ChannelConfig


class DummyChannel(BaseChannel):
    name = "dummy"

    async def send(self, msg):
        return None


def _collect_messages(bus: MessageBus) -> list:
    addr = next(iter(bus.inbound.keys()))
    queue = bus.inbound[addr]
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_channel_config_duration_parsing_and_serialization() -> None:
    cfg = ChannelConfig(
        summon="mention_or_reply",
        summon_lookback="5m",
        summon_max_gap="2 minutes",
    )
    assert cfg.summon_lookback == timedelta(minutes=5)
    assert cfg.summon_max_gap == timedelta(minutes=2)

    dumped = cfg.model_dump()
    assert dumped["summon_lookback"] == "5m"
    assert dumped["summon_max_gap"] == "2m"


def test_summon_filter_buffers_until_summoned() -> None:
    bus = MessageBus()
    cfg = ChannelConfig(summon="mention_or_reply", summon_lookback="5m", summon_max_gap="2m")
    ch = DummyChannel(cfg, bus)
    base = datetime(2025, 1, 1, 10, 0, 0)

    import asyncio

    async def run() -> None:
        await ch._handle_message(
            sender_id="u1",
            chat_id="group",
            content="hello",
            metadata={"is_group": True},
            occurred_at=base,
        )
        await ch._handle_message(
            sender_id="u2",
            chat_id="group",
            content="yo",
            metadata={"is_group": True},
            occurred_at=base + timedelta(seconds=30),
        )
        await ch._handle_message(
            sender_id="u2",
            chat_id="group",
            content="@bot please help",
            metadata={"is_group": True, "_summon_source": "mention"},
            occurred_at=base + timedelta(seconds=60),
        )

    asyncio.run(run())

    msgs = _collect_messages(bus)
    assert [m.content for m in msgs] == ["hello", "yo", "@bot please help"]
    assert all(m.metadata.get("summon") == "mention" for m in msgs)


def test_attention_expires_after_gap() -> None:
    bus = MessageBus()
    cfg = ChannelConfig(summon="mention_or_reply", summon_lookback="5m", summon_max_gap="2m")
    ch = DummyChannel(cfg, bus)
    base = datetime(2025, 1, 1, 11, 0, 0)

    import asyncio

    async def run() -> None:
        await ch._handle_message(
            sender_id="u1",
            chat_id="group",
            content="@bot start",
            metadata={"is_group": True, "_summon_source": "mention"},
            occurred_at=base,
        )
        await ch._handle_message(
            sender_id="u1",
            chat_id="group",
            content="follow up",
            metadata={"is_group": True},
            occurred_at=base + timedelta(minutes=1),
        )
        await ch._handle_message(
            sender_id="u1",
            chat_id="group",
            content="silent after timeout",
            metadata={"is_group": True},
            occurred_at=base + timedelta(minutes=4),
        )

    asyncio.run(run())

    msgs = _collect_messages(bus)
    assert [m.content for m in msgs] == ["@bot start", "follow up"]


def test_private_messages_bypass_summon() -> None:
    bus = MessageBus()
    cfg = ChannelConfig(summon="mention_or_reply")
    ch = DummyChannel(cfg, bus)

    import asyncio

    async def run() -> None:
        await ch._handle_message(
            sender_id="u1",
            chat_id="dm",
            content="hi",
            metadata={"is_group": False},
        )

    asyncio.run(run())

    msgs = _collect_messages(bus)
    assert [m.content for m in msgs] == ["hi"]
