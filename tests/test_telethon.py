"""Actual Telethon adapter with fake clients and real TL objects; never connects."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import TelegramClient, errors, events, functions, types
from telethon.sessions import MemorySession
from telethon_fake import (
    API_HASH,
    API_ID,
    CHANNEL,
    TOKEN,
    FakeClient,
    basic_group,
    channel,
    user,
)

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
    async def test_group_resolution_timeout_and_shutdown_never_send(self):
        started, ended = asyncio.Event(), asyncio.Event()

        async def blocked(peer):
            started.set()
            try:
                await asyncio.Future()
            finally:
                ended.set()

        self.client.get_input_entity = blocked
        with self.assertRaises(TelegramError) as error:
            await self.api.call(
                "sendMessage", dict(chat_id=CHANNEL, text="synthetic"), timeout=0.01
            )
        self.assertEqual(error.exception.kind, "NETWORK")
        self.assertTrue(ended.is_set())
        started.clear()
        ended.clear()
        pending = asyncio.create_task(
            self.api.call("sendMessage", dict(chat_id=CHANNEL, text="synthetic"))
        )
        await started.wait()
        await self.api.close()
        await asyncio.gather(pending, return_exceptions=True)
        self.assertTrue(pending.cancelled())
        self.assertTrue(ended.is_set())
        self.assertFalse(any(kind == "send" for kind, _ in self.client.calls))

    async def test_group_callback_without_cached_chat_type_is_verified_before_enqueue(
        self,
    ):
        self.api.admins = frozenset(["1"])
        for broadcast in (False, True):
            self.client.chat = channel(broadcast=broadcast, megagroup=not broadcast)
            query = types.UpdateBotCallbackQuery(
                query_id=123,
                user_id=1,
                peer=types.PeerChannel(1234567890),
                msg_id=7,
                chat_instance=1,
                data=b"ad:menu",
            )
            item = events.CallbackQuery.Event(query, query.peer, query.msg_id)
            item.get_sender = AsyncMock(return_value=user(1, bot=False))
            self.assertIsNone(item.is_group)
            await self.api._callback(item)
            self.assertEqual(self.api.updates.qsize(), 0 if broadcast else 1)
            if not broadcast:
                update = self.api.updates.get_nowait()
                self.assertEqual(
                    update["callback_query"]["message"]["chat"],
                    dict(id=int(CHANNEL), type="supergroup"),
                )
            else:
                item.get_sender.assert_not_awaited()

    async def test_group_peer_resolution_rejects_mismatch_missing_and_reconfiguration(
        self,
    ):
        for peer in (types.InputPeerUser(1, 10), types.InputPeerChannel(999, 10)):
            self.client.get_input_entity = AsyncMock(return_value=peer)
            with self.assertRaises(TelegramError) as error:
                await self.api.call(
                    "sendMessage", dict(chat_id=CHANNEL, text="synthetic")
                )
            self.assertEqual(error.exception.kind, "INVALID_RESPONSE")
        self.client.get_input_entity = AsyncMock(side_effect=ValueError(TOKEN))
        with self.assertRaises(TelegramError) as error:
            await self.api.call("sendMessage", dict(chat_id=CHANNEL, text="synthetic"))
        self.assertEqual(error.exception.kind, "DESTINATION_UNRESOLVED")
        self.assertNotIn(TOKEN, str(error.exception))

        async def reconfigured(peer):
            self.api.channel = "-100999999"
            return types.InputPeerChannel(1234567890, 10)

        self.client.get_input_entity = AsyncMock(side_effect=reconfigured)
        with self.assertRaises(TelegramError) as error:
            await self.api.call("sendMessage", dict(chat_id=CHANNEL, text="synthetic"))
        self.assertEqual(error.exception.code, 403)
        self.assertFalse(any(kind == "send" for kind, _ in self.client.calls))

    async def test_group_uses_real_telethon_cached_access_hash_for_checks_and_sends(
        self,
    ):
        native = TelegramClient(MemorySession(), API_ID, API_HASH)
        self.addAsyncCleanup(native.disconnect)
        chat = channel(broadcast=False, megagroup=True)
        chat.access_hash = 987654321
        native.session.process_entities([chat])
        self.client.chat = chat
        self.client.get_input_entity = AsyncMock(wraps=native.get_input_entity)

        async def require_full_hash(request):
            if isinstance(request, functions.channels.GetChannelsRequest):
                peer = request.id[0]
            elif isinstance(request, functions.channels.GetParticipantRequest):
                peer = request.channel
            else:
                return
            if peer.access_hash != chat.access_hash:
                raise errors.ChannelInvalidError(request)

        self.client.hook = require_full_hash
        await self.api.initialize()
        with patch.object(
            TelegramClient,
            "__call__",
            AsyncMock(side_effect=AssertionError("No live RPC permitted")),
        ):
            self.assertTrue((await self.api.verify_channel(CHANNEL))["canPost"])
            await self.api.call("sendMessage", dict(chat_id=CHANNEL, text="synthetic"))
            peer = self.client.calls[-1][1][0]
            self.assertEqual(
                (peer.channel_id, peer.access_hash), (chat.id, chat.access_hash)
            )
            await self.api.call(
                "editMessageText", dict(chat_id=CHANNEL, text="edited", message_id=1)
            )
            peer = self.client.calls[-1][1][0]
            self.assertEqual(peer.access_hash, chat.access_hash)

    async def test_basic_and_supergroup_permissions_peers_and_admin_revocation(self):
        for destination, chat in (
            ("-123", basic_group()),
            (CHANNEL, channel(broadcast=False, megagroup=True)),
        ):
            with self.subTest(destination=destination):
                client = FakeClient()
                client.chat = chat
                client.can_post = (
                    None  # post_messages applies only to broadcast channels.
                )
                api = TelegramAPI(
                    TOKEN,
                    API_ID,
                    API_HASH,
                    ["1"],
                    destination,
                    client=client,
                    admin_users=["1"],
                )
                self.addAsyncCleanup(api.close)
                await api.initialize()
                self.assertTrue((await api.verify_channel(destination))["canPost"])
                await api.call(
                    "sendMessage", dict(chat_id=destination, text="synthetic")
                )
                peer = client.calls[-1][1][0]
                self.assertIsInstance(
                    peer,
                    types.InputPeerChat
                    if destination == "-123"
                    else types.InputPeerChannel,
                )
                if destination == "-123":
                    chat.admin_rights = None
                else:
                    client.admin = False
                self.assertFalse((await api.verify_channel(destination))["canPost"])
                for kind, request in client.calls:
                    if kind == "rpc":
                        self.assertNotIsInstance(
                            request, functions.channels.GetParticipantsRequest
                        )
                        if isinstance(
                            request, functions.channels.GetParticipantRequest
                        ):
                            self.assertIsInstance(
                                request.participant, types.InputPeerSelf
                            )
                chat.left = True
                self.assertFalse((await api.verify_channel(destination))["canPost"])

    async def test_group_events_require_configured_chat_and_explicit_admin_before_lookup(
        self,
    ):
        self.api.admins = frozenset(["1"])
        for actor, chat in ((2, int(CHANNEL)), (1, -999), (9, int(CHANNEL))):
            item = event(actor, chat=chat, private=False)
            item.is_group = True
            await self.api._message(item)
            await self.api._callback(item)
            item.get_sender.assert_not_awaited()
        for callback in (False, True):
            item = event(1, chat=int(CHANNEL), private=False)
            item.is_group = True
            item.message.reply_to_msg_id = 17
            await (self.api._callback(item) if callback else self.api._message(item))
        batch = await self.batch()
        self.assertEqual(
            batch[0]["message"]["chat"], dict(id=int(CHANNEL), type="supergroup")
        )
        self.assertEqual(batch[0]["message"]["reply_to_message"]["message_id"], 17)
        self.assertEqual(
            batch[1]["callback_query"]["message"]["chat"]["id"], int(CHANNEL)
        )

    async def test_migrated_or_wrong_basic_group_and_banned_supergroup_fail_closed(
        self,
    ):
        for chat in (basic_group(999), basic_group()):
            client = FakeClient()
            client.chat = chat
            api = TelegramAPI(TOKEN, API_ID, API_HASH, ["1"], "-123", client=client)
            self.addAsyncCleanup(api.close)
            await api.initialize()
            if chat.id == 123:
                chat.migrated_to = types.InputChannel(1234567890, 0)
            self.assertFalse((await api.verify_channel("-123"))["canPost"])
        self.client.chat = channel(broadcast=False, megagroup=True)
        self.client.chat.banned_rights = types.ChatBannedRights(None, send_plain=True)
        await self.api.initialize()
        self.assertFalse((await self.api.verify_channel(CHANNEL))["canPost"])

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
            ("chat", channel(broadcast=False, megagroup=False)),
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
            (errors.ChannelInvalidError(None), 400, "DESTINATION_INVALID", None),
            (errors.ChatIdInvalidError(None), 400, "DESTINATION_INVALID", None),
            (errors.PeerIdInvalidError(None), 400, "DESTINATION_INVALID", None),
            (errors.ChannelPrivateError(None), 403, "DESTINATION_INACCESSIBLE", None),
            (
                errors.UserNotParticipantError(None),
                403,
                "DESTINATION_INACCESSIBLE",
                None,
            ),
            (errors.ChatAdminRequiredError(None), 403, "ADMIN_REQUIRED", None),
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
