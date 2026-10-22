"""Test-only HTTP-shaped simulator for existing business/UI workflows.

A required MockTransport intercepts every request. Production uses only Telethon;
MTProto request/event tests live in test_telethon.py.
"""

import asyncio
import logging
import re
from typing import Any

import httpx

from trade_alert.telegram import TelegramAPI, TelegramError
from trade_alert.validation import integer, loads, obj


class FakeTelegramAPI(TelegramAPI):
    def __init__(self, token: str, *, transport: httpx.MockTransport) -> None:
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{30,}", token):
            raise ValueError("Invalid bot token")
        self._url = f"https://api.telegram.org/bot{token}/"
        self._expected_id = int(token.split(":", 1)[0])
        self.bot_id: int | None = None
        self.username = ""
        # HTTPX's request logger includes the bot token in the URL.
        logging.getLogger("httpx").disabled = True
        self.http = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False
        )
        self.closed = False
        self.active: set[asyncio.Task[Any]] = set()

    async def call(
        self, method: str, payload: dict[str, Any] | None = None, *, timeout: float = 10
    ) -> Any:
        if method not in {
            "getMe",
            "getUpdates",
            "getChat",
            "getChatMember",
            "sendMessage",
            "editMessageText",
            "answerCallbackQuery",
            "setMyCommands",
        }:
            raise ValueError("Unsupported Telegram method")
        if self.closed:
            raise TelegramError(0, "CLOSED")
        task = asyncio.current_task()
        assert task is not None
        if task.cancelling():
            raise asyncio.CancelledError
        self.active.add(task)
        status = 0
        try:
            try:
                async with asyncio.timeout(timeout) as deadline:
                    response = await self.http.post(
                        self._url + method, json=payload or {}, timeout=timeout
                    )
                status = response.status_code
                if deadline.expired():
                    raise TimeoutError
                if self.closed or task.cancelling():
                    raise asyncio.CancelledError
                body = obj(loads(response.text))
                if body.get("ok") is not True or not response.is_success:
                    code = integer(
                        body.get("error_code", response.status_code), 100, 599
                    )
                    retry = obj(body.get("parameters", {})).get("retry_after")
                    retry = (
                        None
                        if retry is None
                        else integer(retry, 0, (2**53 - 1) // 1000)
                    )
                    kind = (
                        "NOT_MODIFIED"
                        if code == 400
                        and "message is not modified"
                        in str(body.get("description", ""))
                        else "API"
                    )
                    raise TelegramError(code, kind, retry)
                return body["result"]
            except TelegramError:
                raise
            except httpx.HTTPError, TimeoutError:
                raise TelegramError(0, "NETWORK") from None
            except ValueError, TypeError, KeyError:
                raise TelegramError(
                    status if status and not 200 <= status < 300 else 0,
                    "INVALID_RESPONSE",
                ) from None
        finally:
            self.active.discard(task)

    async def close(self) -> None:
        self.closed = True
        tasks = tuple(self.active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.http.aclose()
