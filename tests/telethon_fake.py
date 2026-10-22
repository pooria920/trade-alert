"""Fake Telethon client using real TL objects; no sockets, auth or subscribers."""

from datetime import datetime, timezone
from types import SimpleNamespace

from telethon import functions, types

TOKEN = "123456:" + "x" * 35
API_ID = 12345
API_HASH = "a" * 32
CHANNEL = "-1001234567890"


def user(identifier=123456, *, bot=True):
    return types.User(
        id=identifier, bot=bot, username="offline_test_bot" if bot else "human"
    )


def channel(identifier=1234567890, *, broadcast=True, megagroup=False):
    return types.Channel(
        id=identifier,
        title="Synthetic",
        photo=types.ChatPhotoEmpty(),
        date=datetime.now(timezone.utc),
        broadcast=broadcast,
        megagroup=megagroup,
    )


def basic_group(identifier=123, *, admin=True):
    return types.Chat(
        id=identifier,
        title="Synthetic group",
        photo=types.ChatPhotoEmpty(),
        participants_count=0,
        date=datetime.now(timezone.utc),
        version=1,
        admin_rights=types.ChatAdminRights(other=True) if admin else None,
    )


class FakeClient:
    def __init__(self):
        self.handlers = []
        self.connected = False
        self.me = user()
        self.chat = channel()
        self.member = self.me
        self.can_post = True
        self.admin = True
        self.hook = None
        self.calls = []

    def add_event_handler(self, callback, builder):
        self.handlers.append((callback, builder))

    def remove_event_handler(self, callback):
        self.handlers = [(c, b) for c, b in self.handlers if c != callback]

    async def connect(self):
        self.connected = True
        self.calls.append(("connect", None))

    async def sign_in(self, **kwargs):
        self.calls.append(("sign_in", kwargs))

    async def disconnect(self):
        self.connected = False
        self.calls.append(("disconnect", None))

    async def get_me(self):
        self.calls.append(("get_me", None))
        return self.me

    async def get_input_entity(self, peer):
        self.calls.append(("resolve", peer))
        if isinstance(peer, types.PeerChannel):
            return types.InputPeerChannel(peer.channel_id, self.chat.access_hash or 0)
        raise AssertionError("Unexpected peer resolution")

    async def __call__(self, request):
        self.calls.append(("rpc", request))
        if self.hook is not None:
            response = await self.hook(request)
            if response is not None:
                return response
        if isinstance(request, functions.updates.GetStateRequest):
            return types.updates.State(1, 0, datetime.now(timezone.utc), 1, 0)
        if isinstance(
            request,
            (functions.channels.GetChannelsRequest, functions.messages.GetChatsRequest),
        ):
            return SimpleNamespace(chats=[self.chat])
        if isinstance(request, functions.channels.GetParticipantRequest):
            participant = (
                types.ChannelParticipantAdmin(
                    user_id=self.member.id,
                    inviter_id=1,
                    promoted_by=1,
                    date=datetime.now(timezone.utc),
                    admin_rights=types.ChatAdminRights(post_messages=self.can_post),
                )
                if self.admin
                else types.ChannelParticipant(
                    user_id=self.member.id, date=datetime.now(timezone.utc)
                )
            )
            return SimpleNamespace(participant=participant, users=[self.member])
        if isinstance(
            request,
            (
                functions.bots.SetBotCommandsRequest,
                functions.messages.SetBotCallbackAnswerRequest,
            ),
        ):
            return True
        raise AssertionError("Unexpected MTProto request")

    async def send_message(self, peer, message, **kwargs):
        self.calls.append(("send", (peer, message, kwargs)))
        return SimpleNamespace(id=1)

    async def edit_message(self, peer, identifier, message, **kwargs):
        self.calls.append(("edit", (peer, identifier, message, kwargs)))
        return SimpleNamespace(id=identifier)
