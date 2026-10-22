"""Actual Telethon adapter with fake clients and real TL objects; never connects."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import errors, functions, types
from telethon.sessions import MemorySession
from telethon_fake import API_HASH, API_ID, CHANNEL, TOKEN, FakeClient, channel, user

from trade_alert.config import parse_env
from trade_alert.mtproto import TelegramAPI, TelegramError


def event(actor=1, *, chat=None, private=True, callback=False, outgoing=False):
    return SimpleNamespace(
        sender_id=actor,
        chat_id=actor if chat is None else chat,
        is_private=private,
        raw_text="/start",
        message=SimpleNamespace(id=7, out=outgoing),
        data=b"notifications:off",
        query=SimpleNamespace(msg_id=7, query_id=-123),
        get_sender=AsyncMock(return_value=user(actor, bot=False)),
    )


class TelethonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeClient()
        self.api = TelegramAPI(
            TOKEN, API_ID, API_HASH, ["1", "2"], CHANNEL, client=self.client
        )
        self.addAsyncCleanup(self.api.close)

    async def batch(self):
        return await self.api.call("getUpdates", dict(timeout=0))

    async def test_native_client_uses_memory_session_and_disables_request_retries(self):
        # Construct the real client, but forbid connect/auth entirely.
        with tempfile.TemporaryDirectory() as directory:
            before = set(Path(directory).iterdir())
            with patch(
                "trade_alert.mtproto.MemorySession", wraps=MemorySession
            ) as session:
                api = TelegramAPI(TOKEN, API_ID, API_HASH, ["1"])
            self.assertIsInstance(api.client.session, MemorySession)
            session.assert_called_once()
            self.assertEqual(api.client._request_retries, 0)
            self.assertEqual(api.client.flood_sleep_threshold, 0)
            self.assertTrue(api.client._sequential_updates)
            buttons = api._buttons(
                dict(
                    reply_markup=dict(
                        inline_keyboard=[[dict(text="Save", callback_data="save")]]
                    )
                )
            )
            self.assertTrue(bytes(api.client.build_reply_markup(buttons)))
            await api.close()
            self.assertEqual(set(Path(directory).iterdir()), before)

    async def test_explicit_bot_sign_in_and_wrong_identity_is_rejected(self):
        await self.api.initialize()
        self.assertEqual(
            self.client.calls[:2],
            [("connect", None), ("sign_in", dict(bot_token=TOKEN))],
        )
        self.assertEqual(self.api.bot_id, 123456)
        self.client.me = user(999)
        with self.assertRaisesRegex(ValueError, "identity"):
            await self.api.initialize()
        self.client.me = user(123456, bot=False)
        with self.assertRaisesRegex(ValueError, "identity"):
            await self.api.initialize()

    async def test_auth_dc_migration_retries_only_sign_in(self):
        self.client.sign_in = AsyncMock(
            side_effect=[
                errors.UserMigrateError(request=None, capture=4),
                self.client.me,
            ]
        )
        await self.api.initialize()
        self.assertEqual(self.client.sign_in.await_count, 2)
        self.assertFalse(any(kind == "send" for kind, _ in self.client.calls))

    async def test_unauthorized_groups_mismatches_and_outgoing_are_rejected_before_lookup(
        self,
    ):
        for item in (
            event(9),
            event(1, chat=2),
            event(1, chat=-123, private=False),
            event(1, private=False),
            event(1, outgoing=True),
        ):
            await self.api._message(item)
            if not item.message.out:
                await self.api._callback(item)
            item.get_sender.assert_not_awaited()
        self.assertTrue(self.api.updates.empty())
        self.assertFalse(self.client.calls)

    async def test_private_message_and_callback_normalize_without_destination_from_data(
        self,
    ):
        await self.api._message(event())
        await self.api._callback(event(2, callback=True))
        batch = await self.batch()
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch[0]["message"]["chat"], dict(id=1, type="private"))
        self.assertEqual(batch[0]["message"]["from"], dict(id=1, is_bot=False))
        callback = batch[1]["callback_query"]
        self.assertEqual(callback["id"], "-123")
        self.assertEqual(callback["message"]["chat"]["id"], 2)
        self.assertEqual(callback["data"], "notifications:off")
        self.assertEqual([v["update_id"] for v in batch], [1, 2])

    async def test_missing_bot_or_mismatched_sender_and_invalid_callback_data_are_rejected(
        self,
    ):
        for sender in (None, user(1), user(2, bot=False)):
            item = event()
            item.get_sender.return_value = sender
            await self.api._message(item)
        for data in (b"\xff", b"x" * 65, b""):
            item = event()
            item.data = data
            await self.api._callback(item)
            item.get_sender.assert_not_awaited()
        self.assertTrue(self.api.updates.empty())

    async def test_connection_probe_is_required_on_idle_and_failed_probe_keeps_queued_updates(
        self,
    ):
        await self.batch()
        self.assertIsInstance(
            self.client.calls[-1][1], functions.updates.GetStateRequest
        )
        await self.api._message(event())

        async def broken(request):
            raise ConnectionError(TOKEN)

        self.client.hook = broken
        with self.assertRaises(TelegramError) as error:
            await self.batch()
        self.assertEqual(error.exception.kind, "NETWORK")
        self.assertEqual(self.api.updates.qsize(), 1)
        self.client.hook = AsyncMock(return_value=SimpleNamespace())
        with self.assertRaises(TelegramError):
            await self.batch()
        self.assertEqual(self.api.updates.qsize(), 1)
        self.client.hook = None
        self.assertEqual(len(await self.batch()), 1)

    async def test_sender_lookup_swallowing_timeout_cannot_enqueue_late_interaction(
        self,
    ):
        async def late_sender():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return user(1, bot=False)

        item = event()
        item.get_sender.side_effect = late_sender
        original_timeout = asyncio.timeout
        with patch(
            "trade_alert.mtproto.asyncio.timeout",
            side_effect=lambda _: original_timeout(0.01),
        ):
            await self.api._message(item)
        self.assertTrue(self.api.updates.empty())

    async def test_overflow_stops_consumption_instead_of_dropping_silently(self):
        for i in range(1001):
            await self.api._message(event())
        with self.assertRaises(TelegramError) as error:
            await self.batch()
        self.assertEqual(error.exception.kind, "UPDATE_OVERFLOW")
        self.assertEqual(self.api.updates.qsize(), 1000)

    async def test_exact_channel_and_current_admin_posting_permission_use_only_self(
        self,
    ):
        await self.api.initialize()
        self.assertTrue((await self.api.verify_channel(CHANNEL))["canPost"])
        requests = [request for kind, request in self.client.calls if kind == "rpc"]
        self.assertIsInstance(requests[0], functions.channels.GetChannelsRequest)
        self.assertIsInstance(requests[1], functions.channels.GetParticipantRequest)
        self.assertIsInstance(requests[1].participant, types.InputPeerSelf)
        self.assertEqual(requests[0].id[0].channel_id, 1234567890)
        self.assertEqual(requests[0].id[0].access_hash, 0)
        for attr, invalid in (
            ("can_post", False),
            ("admin", False),
            ("member", user(999)),
            ("chat", channel(999)),
            ("chat", channel(broadcast=False, megagroup=True)),
        ):
            original = getattr(self.client, attr)
            setattr(self.client, attr, invalid)
            self.assertFalse((await self.api.verify_channel(CHANNEL))["canPost"])
            setattr(self.client, attr, original)
        self.assertFalse(any(kind == "send" for kind, _ in self.client.calls))

    async def test_unknown_private_or_channel_destination_fails_before_any_send(self):
        for target in ("3", "@channel", "-1009999999999"):
            with self.assertRaises(TelegramError) as error:
                await self.api.call("sendMessage", dict(chat_id=target, text="test"))
            self.assertEqual(error.exception.code, 403)
        self.assertFalse(self.client.calls)

    async def test_plain_text_buttons_edit_callback_answer_and_command_scopes(self):
        payload = dict(
            chat_id="1",
            text="<x>_*",
            reply_markup=dict(
                inline_keyboard=[[dict(text="Button", callback_data="aw:token:save")]]
            ),
        )
        await self.api.call("sendMessage", payload)
        peer, message, options = self.client.calls[-1][1]
        self.assertIsInstance(peer, types.InputPeerUser)
        self.assertEqual((peer.user_id, peer.access_hash), (1, 0))
        self.assertEqual(message, "<x>_*")
        self.assertIsNone(options["parse_mode"])
        self.assertFalse(options["link_preview"])
        self.assertEqual(options["buttons"][0][0].type.data, b"aw:token:save")
        await self.api.call("editMessageText", dict(payload, message_id=7))
        self.assertEqual(self.client.calls[-1][0], "edit")
        await self.api.call(
            "answerCallbackQuery", dict(callback_query_id="-123", text="done")
        )
        self.assertIsInstance(
            self.client.calls[-1][1], functions.messages.SetBotCallbackAnswerRequest
        )
        self.assertEqual(self.client.calls[-1][1].query_id, -123)
        for scope, cls in (
            (dict(type="all_private_chats"), types.BotCommandScopeUsers),
            (dict(type="chat", chat_id="1"), types.BotCommandScopePeer),
        ):
            await self.api.call(
                "setMyCommands",
                dict(
                    commands=[dict(command="start", description="Start")], scope=scope
                ),
            )
            self.assertIsInstance(self.client.calls[-1][1].scope, cls)

    async def test_flood_and_rpc_errors_are_safe_and_never_retried_by_adapter(self):
        for error, code, kind, retry in (
            (errors.FloodWaitError(None, capture=12), 429, "API", 12),
            (errors.FloodPremiumWaitError(None, capture=20), 429, "API", 20),
            (errors.MessageNotModifiedError(None), 400, "NOT_MODIFIED", None),
            (errors.AuthKeyDuplicatedError(None), 409, "SESSION_CONFLICT", None),
            (errors.ChatWriteForbiddenError(None), 403, "API", None),
            (errors.ServerError(None, TOKEN, 500), 500, "API", None),
        ):
            self.client.send_message = AsyncMock(side_effect=error)
            with self.assertRaises(TelegramError) as result:
                await self.api.call("sendMessage", dict(chat_id="1", text="test"))
            self.assertEqual(
                (
                    result.exception.code,
                    result.exception.kind,
                    result.exception.retry_after,
                ),
                (code, kind, retry),
            )
            self.assertNotIn(TOKEN, str(result.exception))
            self.client.send_message.assert_awaited_once()

    async def test_deadline_and_shutdown_cancel_and_join_send_and_sender_lookup(self):
        started, ended = asyncio.Event(), asyncio.Event()

        async def blocked(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                ended.set()

        self.client.send_message = blocked
        with self.assertRaises(TelegramError) as error:
            await self.api.call(
                "sendMessage", dict(chat_id="1", text="test"), timeout=0.01
            )
        self.assertEqual(error.exception.kind, "NETWORK")
        self.assertTrue(ended.is_set())
        started.clear()
        ended.clear()
        item = event()
        item.get_sender.side_effect = blocked
        task = asyncio.create_task(self.api._message(item))
        await started.wait()
        await asyncio.wait_for(self.api.close(), 1)
        self.assertTrue(ended.is_set())
        self.assertTrue(task.done())
        self.assertFalse(self.client.handlers)
        self.assertFalse(self.client.connected)
        self.assertTrue(self.api.updates.empty())

    def test_credentials_validation_reports_field_names_without_values(self):
        required = dict(
            TELEGRAM_BOT_TOKEN=TOKEN,
            TELEGRAM_ALLOWED_USER_IDS="1",
            TELEGRAM_API_ID=str(API_ID),
            TELEGRAM_API_HASH=API_HASH,
        )
        for field, invalid in (
            ("TELEGRAM_API_ID", ""),
            ("TELEGRAM_API_ID", "1.5"),
            ("TELEGRAM_API_ID", str(2**31)),
            ("TELEGRAM_API_HASH", "secret"),
            ("TELEGRAM_API_HASH", "g" * 32),
        ):
            with self.assertRaises(ValueError) as error:
                parse_env({**required, field: invalid})
            self.assertEqual(
                str(error.exception), f"Invalid environment variables: {field}"
            )
        for field in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
            values = dict(required)
            values.pop(field)
            with self.assertRaisesRegex(ValueError, field):
                parse_env(values)
