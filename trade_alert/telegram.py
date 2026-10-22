"""Authorization-bound reply context; Telegram I/O is implemented with Telethon."""

from dataclasses import dataclass
from typing import Any

from .mtproto import TelegramAPI as TelegramAPI
from .mtproto import TelegramError as TelegramError
from .users import UserService

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

    async def answer(self, notice: str = "") -> None:
        if self.callback_id:
            self.users.authorize(self.actor)
            await self.api.call(
                "answerCallbackQuery",
                dict(callback_query_id=self.callback_id, text=notice),
            )

    async def reply(
        self, message: str, buttons: Buttons | None = None, *, edit: bool = False
    ) -> None:
        self.users.authorize(self.actor)
        preferences = self.users.preferences(self.actor)
        if preferences is None or preferences["private_chat_id"] != self.actor:
            raise PermissionError("An observed matching private chat is required")
        payload: dict[str, Any] = dict(chat_id=self.actor, text=message)
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
                await self.api.call(
                    "editMessageText", dict(**payload, message_id=self.message_id)
                )
                return
            except TelegramError as error:
                if error.kind == "NOT_MODIFIED":
                    return
        # Split plain text without losing content; no Telegram parse mode is used.
        # 1,750 Unicode characters fit even when each occupies two UTF-16 units.
        for offset in range(0, len(message), 1750):
            await self.api.call(
                "sendMessage", dict(payload, text=message[offset : offset + 1750])
            )
