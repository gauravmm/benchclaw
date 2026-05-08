"""Typing-indicator refresh loop (per-chat).

Each chat gets at most one typing task; ``start`` cancels any predecessor
so a typing bubble in chat A never suppresses one in chat B.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from telegram.ext import Application


class TypingManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._app: "Application | None" = None

    def attach(self, app: "Application") -> None:
        self._app = app

    def detach(self) -> None:
        for chat_id in list(self._tasks):
            self.stop(chat_id)
        self._app = None

    async def start(self, chat_id: str) -> None:
        """Send the initial chat_action inline before spawning the refresher.

        Awaiting the first send keeps a typing bubble visible even when the
        LLM replies in <1s (the refresher would otherwise still be scheduling
        when send completes).
        """
        if not self._app:
            return
        self.stop(chat_id)
        try:
            await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
        except Exception as e:
            logger.debug(f"Initial typing action failed for {chat_id}: {e}")
        self._tasks[chat_id] = asyncio.create_task(self._loop(chat_id))

    def stop(self, chat_id: str) -> None:
        task = self._tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _loop(self, chat_id: str) -> None:
        try:
            while self._app:
                await asyncio.sleep(4)
                await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Typing indicator stopped for {chat_id}: {e}")
