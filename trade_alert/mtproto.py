"""Telethon bot transport. Bot-shaped method names are local compatibility only."""

import asyncio
import logging
import re
import warnings
from collections.abc import Sequence
from typing import Any

from telethon import (
    Button,
    TelegramClient,
    connection,
    errors,
    events,
    functions,
    types,
    utils,
)
from telethon.sessions import MemorySession

from .config import channel_id, mtproxy, user_ids
from .mtproxy_tls import ConnectionTcpMTProxyFakeTLS
from .proxy_auto import ProxyPool, automatic_connection
from .validation import integer, obj, text

UPDATE_WAIT_SECONDS = 30


class TelegramError(Exception):
    def __init__(
        self, code: int, kind: str = "API", retry_after: int | None = None
    ) -> None:
        super().__init__(f"Telegram {kind} {code}")
        self.code, self.kind, self.retry_after = code, kind, retry_after


class TelegramAPI:
    def __init__(
        self,
        token: str,
        api_id: int,
        api_hash: str,
        allowed_users: Sequence[str],
        configured_channel: str = "",
        *,
        client: Any = None,
        admin_users: Sequence[str] = (),
        proxy_url: str = "",
        auto_proxy: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{30,}", token):
            raise ValueError("Invalid bot token")
        integer(api_id, 1, 2**31 - 1)
        if not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise ValueError("Invalid Telegram API credentials")
        for actor in allowed_users:
            if user_ids(actor) != [actor]:
                raise ValueError("Invalid authorized user ID")
        self.allowed = frozenset(allowed_users)
        if not set(admin_users).issubset(self.allowed):
            raise ValueError("Group admins must be authorized users")
        self.admins = frozenset(admin_users)
        self.channel = channel_id(configured_channel) if configured_channel else ""
        self._token, self._expected_id = token, int(token.split(":", 1)[0])
        self.bot_id: int | None = None
        self.username = ""
        self.closed, self.started = False, False
        self.active: set[asyncio.Task[Any]] = set()
        # Telethon's default exception/request logging can contain credentials or peers.
        quiet = logging.getLogger("trade_alert.telethon")
        quiet.setLevel(100)
        quiet.propagate = False
        quiet.addHandler(logging.NullHandler())
        proxy = mtproxy(proxy_url)
        self.auto_proxy = auto_proxy and not proxy and client is None
        self._proxy_failed = False
        self._connection_lock = asyncio.Lock()
        self.logger = logger
        transport: dict[str, Any] = {}
        if self.auto_proxy:
            transport["connection"] = automatic_connection(ProxyPool())
        if proxy:
            host, port, secret = proxy
            # Prefix once so Telethon does not strip bytes from a raw key starting dd/ee.
            transport = dict(
                connection=ConnectionTcpMTProxyFakeTLS
                if len(secret) > 34
                else connection.ConnectionTcpMTProxyRandomizedIntermediate,
                proxy=(host, port, secret if len(secret) > 34 else "dd" + secret[-32:]),
            )
        with warnings.catch_warnings():
            if proxy:
                # Telethon 1.45 checks SOCKS dependencies even for native MTProxy.
                warnings.filterwarnings(
                    "ignore",
                    message="proxy argument will be ignored because python-socks is not installed",
                    category=UserWarning,
                    module=r"telethon\.client\.telegrambaseclient",
                )
            self.client = (
                client
                if client is not None
                else TelegramClient(
                    MemorySession(),
                    api_id,
                    api_hash,
                    request_retries=0,
                    connection_retries=3,
                    retry_delay=1,
                    timeout=10,
                    flood_sleep_threshold=0,
                    raise_last_call_error=True,
                    sequential_updates=True,
                    base_logger=quiet,
                    **transport,
                )
            )
        # No session credential or entity/subscriber database is written to disk.
        # Bots may use zero access hashes for peers absent from their local cache.
        self.client.add_event_handler(self._message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self._callback, events.CallbackQuery())
        # ponytail: fail closed at 1,000 queued authorized interactions; revisit under measured load.
        self.updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self.ready = asyncio.Event()
        self.sequence, self.overflow = 0, False

    def _cancelled(self) -> None:
        task = asyncio.current_task()
        if self.closed or (task is not None and task.cancelling()):
            raise asyncio.CancelledError

    async def _message(self, event: Any) -> None:
        await self._receive(event, callback=False)

    async def _callback(self, event: Any) -> None:
        await self._receive(event, callback=True)

    async def _receive(self, event: Any, *, callback: bool) -> None:
        """Reject unknown peers and non-admin group actors before sender lookup."""
        task = asyncio.current_task()
        assert task is not None
        self.active.add(task)
        try:
            self._cancelled()
            actor = integer(event.sender_id, 1, 2**52 - 1)
            private = event.is_private is True
            group_type = getattr(event, "is_group", False)
            group = (
                (group_type is True or group_type is None)
                and str(actor) in self.admins
                and str(event.chat_id) == self.channel
            )
            if (
                str(actor) not in self.allowed
                or type(event.chat_id) is not int
                or not ((private and event.chat_id == actor) or (not private and group))
            ):
                return
            if callback:
                data = bytes(event.data)
                if not 1 <= len(data) <= 64:
                    return
                content = data.decode("utf-8", errors="strict")
                message_id = integer(event.query.msg_id, 1)
                callback_id = str(integer(event.query.query_id, -(2**63), 2**63 - 1))
            else:
                if event.message.out:
                    return
                content = text(event.raw_text or "", 4096, trim=False, minimum=0)
                message_id = integer(event.message.id, 1)
            async with asyncio.timeout(10) as deadline:
                if not private and group_type is None:
                    # Callback updates may omit the chat entity entirely. Resolve
                    # only the exact configured peer after the actor/scope guard.
                    chat = await self._chat(self.channel)
                    if self._chat_type(chat) not in ("group", "supergroup") or str(
                        utils.get_peer_id(chat)
                    ) != str(event.chat_id):
                        return
                sender = await event.get_sender()
            self._cancelled()
            if deadline.expired():
                return
            if not isinstance(sender, types.User) or sender.id != actor or sender.bot:
                return
            message = dict(
                message_id=message_id,
                chat=dict(
                    id=event.chat_id,
                    type="private"
                    if private
                    else "group"
                    if utils.resolve_id(event.chat_id)[1] is types.PeerChat
                    else "supergroup",
                ),
            )
            reply = (
                getattr(event.message, "reply_to_msg_id", None)
                if not callback
                else None
            )
            if reply is not None:
                message["reply_to_message"] = dict(message_id=integer(reply, 1))
            human = dict(id=actor, is_bot=False)
            self.sequence += 1
            if callback:
                update = dict(
                    update_id=self.sequence,
                    callback_query=dict(
                        id=callback_id, data=content, **{"from": human}, message=message
                    ),
                )
            else:
                update = dict(
                    update_id=self.sequence,
                    message=dict(message, text=content, **{"from": human}),
                )
            self.updates.put_nowait(update)
            self.ready.set()
        except asyncio.QueueFull:
            self.overflow = True
            self.ready.set()
        except Exception:
            # Never let Telethon log raw events, sender lookups or RPC exceptions.
            return
        finally:
            self.active.discard(task)

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
        self._cancelled()
        task = asyncio.current_task()
        assert task is not None
        self.active.add(task)
        try:
            async with asyncio.timeout(timeout) as deadline:
                result = await self._request(method, payload or {})
            self._cancelled()
            if deadline.expired():
                raise TimeoutError
            return result
        except (
            errors.FloodWaitError,
            errors.FloodPremiumWaitError,
            errors.SlowModeWaitError,
            errors.FloodTestPhoneWaitError,
        ) as error:
            raise TelegramError(
                429, retry_after=integer(error.seconds, 0, (2**53 - 1) // 1000)
            ) from None
        except errors.MessageNotModifiedError:
            raise TelegramError(400, "NOT_MODIFIED") from None
        except errors.AuthKeyDuplicatedError:
            raise TelegramError(409, "SESSION_CONFLICT") from None
        except (
            errors.ChannelInvalidError,
            errors.ChatIdInvalidError,
            errors.PeerIdInvalidError,
        ):
            raise TelegramError(400, "DESTINATION_INVALID") from None
        except errors.ChannelPrivateError, errors.UserNotParticipantError:
            raise TelegramError(403, "DESTINATION_INACCESSIBLE") from None
        except errors.ChatAdminRequiredError:
            raise TelegramError(403, "ADMIN_REQUIRED") from None
        except errors.RPCError as error:
            # A 420 MTProto flood error must never become a permanent outbox failure.
            code = int(error.code or 0)
            raise TelegramError(429 if code == 420 else code) from None
        except OSError, TimeoutError:
            if self.auto_proxy:
                self._proxy_failed = True
            raise TelegramError(0, "NETWORK") from None
        except ValueError, TypeError, KeyError, AttributeError:
            raise TelegramError(0, "INVALID_RESPONSE") from None
        finally:
            self.active.discard(task)

    async def _peer(self, value: str) -> Any:
        if value == self.channel and value:
            identifier, kind = utils.resolve_id(int(value))
            if kind is types.PeerChat:
                return types.InputPeerChat(identifier)
            # Let Telethon reuse the current session's full access hash. Its bot
            # zero-hash discovery is a fallback, not a replacement for known hashes.
            try:
                peer = await self.client.get_input_entity(types.PeerChannel(identifier))
            except ValueError:
                raise TelegramError(400, "DESTINATION_UNRESOLVED") from None
            self._cancelled()
            if value != self.channel:
                raise TelegramError(403)
            if (
                not isinstance(peer, types.InputPeerChannel)
                or str(utils.get_peer_id(peer)) != value
            ):
                raise TelegramError(0, "INVALID_RESPONSE")
            return peer
        if value not in self.allowed:
            raise TelegramError(403)
        return types.InputPeerUser(int(value), 0)

    async def _chat(self, value: str) -> Any:
        if not self.channel or value != self.channel:
            raise TelegramError(403)
        peer = await self._peer(value)
        request = (
            functions.messages.GetChatsRequest([peer.chat_id])
            if isinstance(peer, types.InputPeerChat)
            else functions.channels.GetChannelsRequest([utils.get_input_channel(peer)])
        )
        response = await self.client(request)
        if len(response.chats) != 1:
            raise ValueError("Unexpected destination result")
        return response.chats[0]

    @staticmethod
    def _chat_type(chat: Any) -> str:
        if getattr(chat, "left", False) or getattr(chat, "deactivated", False):
            return "invalid"
        if isinstance(chat, types.Chat) and not chat.migrated_to:
            return "group"
        if isinstance(chat, types.Channel):
            banned = chat.banned_rights
            if banned and (
                banned.view_messages or banned.send_messages or banned.send_plain
            ):
                return "invalid"
            if chat.megagroup and not chat.broadcast:
                return "supergroup"
            if chat.broadcast and not chat.megagroup:
                return "channel"
        return "invalid"

    @staticmethod
    def _buttons(payload: dict[str, Any]) -> Any:
        markup = payload.get("reply_markup")
        if markup is None:
            return None
        result = []
        for row in obj(markup)["inline_keyboard"]:
            buttons = []
            for button in row:
                data = text(button["callback_data"], 64, trim=False).encode("utf-8")
                if len(data) > 64:
                    raise ValueError("Invalid callback length")
                buttons.append(Button.inline(text(button["text"], 128), data))
            result.append(buttons)
        return result

    async def _request(self, method: str, p: dict[str, Any]) -> Any:
        if self.auto_proxy and method != "getMe":
            async with self._connection_lock:
                if self._proxy_failed:
                    await self.client.disconnect()
                if self._proxy_failed or not self.client.is_connected():
                    await self.client.connect()
                self._proxy_failed = False
        if method == "getMe":
            if not self.started:
                await self.client.connect()
                # Explicit bot sign-in never prompts for a phone, password or login code.
                for attempt in range(3):
                    try:
                        await self.client.sign_in(bot_token=self._token)
                        break
                    except (
                        errors.UserMigrateError,
                        errors.PhoneMigrateError,
                        errors.NetworkMigrateError,
                    ):
                        # Telethon already switched DC; retry authentication only.
                        if attempt == 2:
                            raise
                self.started = True
            me = await self.client.get_me()
            self._proxy_failed = False
            return dict(id=me.id, is_bot=me.bot is True, username=me.username)
        if method == "getUpdates":
            # Local queue consumption, not HTTP long polling. The idle probe is a
            # real MTProto response; a quiet socket or timer alone is not health.
            if not self.overflow and self.updates.empty():
                try:
                    await asyncio.wait_for(
                        self.ready.wait(),
                        min(UPDATE_WAIT_SECONDS, p.get("timeout", UPDATE_WAIT_SECONDS)),
                    )
                except TimeoutError:
                    pass
            if self.overflow:
                raise TelegramError(409, "UPDATE_OVERFLOW")
            state = await self.client(functions.updates.GetStateRequest())
            if not isinstance(state, types.updates.State):
                raise ValueError("Invalid Telegram connection probe")
            self._cancelled()
            batch: list[dict[str, Any]] = []
            while not self.updates.empty() and len(batch) < 100:
                batch.append(self.updates.get_nowait())
            if self.updates.empty():
                self.ready.clear()
            return batch
        if method == "getChat":
            chat = await self._chat(str(p["chat_id"]))
            return dict(
                id=utils.get_peer_id(chat),
                type=self._chat_type(chat),
                title=chat.title,
            )
        if method == "getChatMember":
            if p["user_id"] != self.bot_id:
                raise TelegramError(403)
            peer = await self._peer(str(p["chat_id"]))
            if isinstance(peer, types.InputPeerChat):
                chat = await self._chat(str(p["chat_id"]))
                admin = (
                    self._chat_type(chat) == "group"
                    and str(utils.get_peer_id(chat)) == str(p["chat_id"])
                    and bool(chat.admin_rights or chat.creator)
                )
                return dict(
                    status="administrator" if admin else "member",
                    can_post_messages=admin,
                    user=dict(id=self.bot_id, is_bot=True),
                )
            result = await self.client(
                functions.channels.GetParticipantRequest(
                    utils.get_input_channel(peer), types.InputPeerSelf()
                )
            )
            participant = result.participant
            admin = isinstance(participant, types.ChannelParticipantAdmin)
            user = next(
                (
                    u
                    for u in result.users
                    if u.id == getattr(participant, "user_id", None)
                ),
                None,
            )
            return dict(
                status="administrator" if admin else "member",
                can_post_messages=admin
                and participant.admin_rights.post_messages is True,
                user=dict(
                    id=getattr(user, "id", None),
                    is_bot=getattr(user, "bot", False) is True,
                ),
            )
        if method in ("sendMessage", "editMessageText"):
            peer = await self._peer(str(p["chat_id"]))
            options = dict(
                parse_mode=None, link_preview=False, buttons=self._buttons(p)
            )
            if method == "sendMessage":
                message = await self.client.send_message(peer, p["text"], **options)
            else:
                message = await self.client.edit_message(
                    peer, integer(p["message_id"], 1), p["text"], **options
                )
            return dict(message_id=message.id)
        if method == "answerCallbackQuery":
            return await self.client(
                functions.messages.SetBotCallbackAnswerRequest(
                    int(p["callback_query_id"]), 0, message=p.get("text", "")
                )
            )
        scope = p.get("scope", {})
        kind = scope.get("type", "all_private_chats")
        if kind == "all_private_chats":
            target = types.BotCommandScopeUsers()
        elif kind == "chat":
            target = types.BotCommandScopePeer(await self._peer(str(scope["chat_id"])))
        else:
            raise ValueError("Unsupported command scope")
        return await self.client(
            functions.bots.SetBotCommandsRequest(
                target,
                "",
                [
                    types.BotCommand(c["command"], c["description"])
                    for c in p["commands"]
                ],
            )
        )

    async def initialize(self) -> None:
        next_notice = 0.0
        while True:
            try:
                value = obj(
                    await self.call("getMe", timeout=45 if self.auto_proxy else 10)
                )
                break
            except TelegramError as error:
                if not self.auto_proxy or error.kind != "NETWORK":
                    raise
                if self.logger and asyncio.get_running_loop().time() >= next_notice:
                    self.logger.warning(
                        "Automatic Telegram proxy connection unavailable; retrying"
                    )
                    next_notice = asyncio.get_running_loop().time() + 60
                await self.client.disconnect()
                self.started = False
                await asyncio.sleep(5)
        bot_id = integer(value["id"], 1, 2**52 - 1)
        if value.get("is_bot") is not True or bot_id != self._expected_id:
            raise ValueError("Unexpected bot identity")
        self.username, self.bot_id = text(value["username"], 32), bot_id

    async def verify_channel(self, configured: str) -> dict[str, Any]:
        configured = channel_id(configured)
        if self.bot_id is None:
            raise RuntimeError("Initialize the bot before checking permissions")
        async with asyncio.timeout(10) as deadline:
            chat = obj(await self.call("getChat", dict(chat_id=configured)))
            if (
                type(chat.get("id")) is not int
                or str(chat["id"]) != configured
                or chat.get("type") not in ("channel", "group", "supergroup")
            ):
                return dict(channelId=configured, title="مقصد نامعتبر", canPost=False)
            member = obj(
                await self.call(
                    "getChatMember",
                    dict(
                        chat_id=configured,
                        user_id=self.bot_id,
                    ),
                )
            )
        self._cancelled()
        if deadline.expired():
            raise TimeoutError
        return dict(
            channelId=configured,
            title=text(chat["title"], 255),
            canPost=member.get("status") == "administrator"
            and (chat["type"] != "channel" or member.get("can_post_messages") is True)
            and type(obj(member.get("user", {})).get("id")) is int
            and obj(member.get("user", {})).get("id") == self.bot_id
            and obj(member.get("user", {})).get("is_bot") is True,
        )

    async def close(self) -> None:
        self.closed = True
        self.client.remove_event_handler(self._message)
        self.client.remove_event_handler(self._callback)
        tasks = tuple(self.active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.disconnect()
