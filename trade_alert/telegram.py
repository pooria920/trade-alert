"""Authorization-bound reply context; Telegram I/O is implemented with Telethon."""

import asyncio
from dataclasses import dataclass
from typing import Any

from .channels import ChannelService
from .mtproto import TelegramAPI as TelegramAPI
from .mtproto import TelegramError as TelegramError
from .users import UserService
from .validation import integer, obj

type Buttons = list[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class Context:
    api: TelegramAPI
    users: UserService
    actor: str
    message_text: str = ""
    data: str = ""
    callback_id: str | None = None
    message_id: int | None = None
    group_id: str | None = None
    channels: ChannelService | None = None
    reply_to_message_id: int | None = None

    @property
    def scope(self) -> str:
        return f"{self.group_id}:{self.actor}" if self.group_id else self.actor

    def target(self) -> str:
        self.users.authorize(self.actor)
        preferences = self.users.preferences(self.actor)
        if preferences is None or preferences["private_chat_id"] != self.actor:
            raise PermissionError("An observed matching private chat is required")
        if self.group_id:
            if self.channels is None or self.group_id != self.channels.channel_id:
                raise PermissionError("Group is no longer configured")
            self.channels.authorize(self.actor)
            return self.channels.channel_id
        return self.actor

    async def answer(self, notice: str = "") -> None:
        if self.callback_id:
            self.target()
            await self.api.call(
                "answerCallbackQuery",
                dict(callback_query_id=self.callback_id, text=notice),
            )

    async def reply(
        self, message: str, buttons: Buttons | None = None, *, edit: bool = False
    ) -> int | None:
        self.target()
        if self.group_id:
            assert self.channels is not None
            async with asyncio.timeout(10):
                if not await self.channels.refresh():
                    raise PermissionError("Group posting permission must be verified")
                return await self._reply(message, buttons, edit=edit)
        return await self._reply(message, buttons, edit=edit)

    async def _reply(
        self, message: str, buttons: Buttons | None, *, edit: bool
    ) -> int | None:
        payload: dict[str, Any] = dict(chat_id=self.target(), text=message)
        if buttons is not None:
            for _, data in buttons:
                if not 1 <= len(data.encode("utf-8")) <= 64:
                    raise ValueError("Invalid callback data length")
            payload["reply_markup"] = dict(
                inline_keyboard=[
                    [dict(text=label, callback_data=data)] for label, data in buttons
                ]
            )
        if edit and self.callback_id and self.message_id is not None:
            try:
                result = await self.api.call(
                    "editMessageText", dict(**payload, message_id=self.message_id)
                )
                self.target()
                return integer(obj(result)["message_id"], 1) if self.group_id else None
            except TelegramError as error:
                if error.kind == "NOT_MODIFIED":
                    return self.message_id if self.group_id else None
        # Split plain text without losing content; no Telegram parse mode is used.
        # 1,750 Unicode characters fit even when each occupies two UTF-16 units.
        last_id = None
        for offset in range(0, len(message), 1750):
            self.target()
            result = await self.api.call(
                "sendMessage", dict(payload, text=message[offset : offset + 1750])
            )
            self.target()
            if self.group_id:
                last_id = integer(obj(result)["message_id"], 1)
        return last_id
