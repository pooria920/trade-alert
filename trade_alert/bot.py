"""Authorized private/group commands and serial consumption of Telethon events."""

import asyncio
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .admin import AdminCommands
from .alerts import AlertService
from .bot_text import (
    LABELS,
    LIVE_NOTICE,
    PRIVATE_TYPES,
    fa,
    history_text,
    minimum_prompt,
    monitoring_text,
    numeric,
    numeric_error,
    tehran_time,
    tree_text,
)
from .channels import ChannelService
from .definitions import definition_json, parse_tree
from .market import Instrument
from .market_service import MarketService
from .telegram import Buttons, Context, TelegramAPI, TelegramError
from .tsetmc import normalize_persian
from .users import UserService, now_ms
from .validation import integer, obj, text

HELP = "دستورهای ربات:\n/start شروع و تنظیم اعلان‌ها\n/help راهنما\n/add خودرو جست‌وجو و افزودن نماد\n/list فهرست شخصی\n/queue خودرو بررسی صف\n/alert ساخت هشدار کاهش یا از بین رفتن صف خرید و فروش\n/alerts مدیریت هشدارها\n/history ده هشدار اخیر\n/remove خودرو حذف نماد و هشدارهای خصوصی آن\n/remove rule شناسه حذف هشدار\n/status وضعیت پایش مشترک\n/cancel لغو فرایند\nپایش طبق نشست تنظیم‌شده است. هشدار فقط با دادهٔ تازه و قابل تأیید صادر می‌شود."
EXPIRED = "این انتخاب منقضی یا تغییر کرده است. از آخرین پیام یا /alert استفاده کنید."


@dataclass(slots=True)
class PrivateDraft:
    expires: int
    token: str = ""
    step: str = "symbol"
    ins_code: str = ""
    kind: str = ""
    window: int | None = None
    percent: float | None = None
    minimum: int | None = None
    rule_id: str | None = None
    revision: int | None = None

    def definition(self) -> dict[str, Any]:
        config = (
            dict(
                thresholdPercent=self.percent,
                windowMinutes=self.window,
                minimumBaselineVolume=self.minimum,
            )
            if self.kind.endswith("DROP_PERCENT")
            else dict(minimumPreviousVolume=self.minimum)
        )
        return definition_json(
            parse_tree(dict(type=self.kind, config=config), channel=False)
        )


@dataclass(slots=True)
class Search:
    expires: int
    instruments: list[Instrument] = field(default_factory=list)
    awaiting_input: bool = False


class TelegramBot:
    def __init__(
        self,
        api: TelegramAPI,
        users: UserService,
        market: MarketService,
        alerts: AlertService,
        get_status: Callable[[], dict[str, Any]],
        channels: ChannelService | None = None,
        *,
        now: Callable[[], int] = now_ms,
        logger: logging.Logger | None = None,
    ) -> None:
        self.api, self.users, self.market, self.alerts = api, users, market, alerts
        self.get_status, self.channels, self.now = get_status, channels, now
        self.logger = logger or logging.getLogger(__name__)
        self.admin = (
            AdminCommands(channels, market, now) if channels is not None else None
        )
        # ponytail: one expiring draft/search per authorized user; restart discards drafts.
        self.flows: dict[str, PrivateDraft] = {}
        self.searches: dict[str, Search] = {}
        self._task: asyncio.Task[Any] | None = None
        self.offset = 0
        self.last_telegram_success: int | None = None
        self.telegram_errors = 0

    def _authorize(self, update: object) -> Context | None:
        """First update middleware. No registration, API call or handler before it."""
        try:
            raw = obj(update)
            integer(raw["update_id"])
            callback = raw.get("callback_query")
            if callback is not None:
                callback = obj(callback)
                message = obj(callback["message"])
                sender = obj(callback["from"])
                callback_id = text(callback["id"], 256, trim=False)
                data = text(callback["data"], 64, trim=False)
                if len(data.encode("utf-8")) > 64:
                    return None
            else:
                message = obj(raw["message"])
                sender = obj(message["from"])
                callback_id, data = None, ""
            actor = str(integer(sender["id"], 1, 2**52 - 1))
            chat = obj(message["chat"])
            if (
                sender.get("is_bot") is not False
                or actor not in self.users.allowed
                or type(chat.get("id")) is not int
            ):
                return None
            group_id = None
            if chat.get("type") == "private":
                if str(chat["id"]) != actor:
                    return None
            elif chat.get("type") in ("group", "supergroup") and self.channels:
                if str(chat["id"]) != self.channels.channel_id:
                    return None
                self.channels.authorize(actor)
                group_id = self.channels.channel_id
            else:
                return None
            message_id = integer(message["message_id"], 1)
            message_text = text(message.get("text", ""), 4096, trim=False, minimum=0)
            return Context(
                self.api,
                self.users,
                actor,
                message_text,
                data,
                callback_id,
                message_id,
                group_id,
                self.channels,
                integer(obj(message["reply_to_message"])["message_id"], 1)
                if "reply_to_message" in message
                else None,
            )
        except ValueError, TypeError, KeyError, OverflowError, PermissionError:
            return None

    async def handle_update(self, update: object) -> None:
        ctx = self._authorize(update)
        if ctx is None:
            return
        try:
            if not ctx.group_id:
                self.users.register(ctx.actor, ctx.actor)
            if ctx.callback_id:
                if ctx.data.startswith(("ad:", "af:")) and self.admin is not None:
                    if (
                        not ctx.group_id
                        and self.channels
                        and self.channels.is_admin(ctx.actor)
                        and (ctx.data == "ad:new" or ctx.data.startswith("ad:edit:"))
                        and self.flows.pop(ctx.actor, None) is not None
                    ):
                        self.searches.pop(ctx.actor, None)
                        await ctx.reply(
                            "پیش‌نویس خصوصی قبلی کنار گذاشته شد؛ مدیریت مقصد مشترک شروع می‌شود."
                        )
                    await self.admin.callback(ctx)
                    return
                if ctx.group_id:
                    await ctx.answer("این دکمه مربوط به گفت‌وگوی خصوصی است.")
                    return
                await ctx.answer()
                await self._callback(ctx)
                return
            command = re.match(
                r"^/([a-zA-Z0-9_]+)(?:@([a-zA-Z0-9_]+))?(?:\s+(.*))?$",
                ctx.message_text,
                re.S,
            )
            if command:
                if command[2] and command[2].casefold() != self.api.username.casefold():
                    return
                if command[1].lower() in ("alert", "admin", "cancel"):
                    replaced = (not ctx.group_id and ctx.actor in self.flows) or bool(
                        self.admin and ctx.scope in self.admin.drafts
                    )
                    if not ctx.group_id:
                        self.flows.pop(ctx.actor, None)
                        self.searches.pop(ctx.actor, None)
                    if self.admin:
                        self.admin.drafts.pop(ctx.scope, None)
                    if replaced and command[1].lower() != "cancel":
                        await ctx.reply(
                            "پیش‌نویس قبلی کنار گذاشته شد؛ فرایند جدید شروع می‌شود."
                        )
                if ctx.group_id:
                    await self._group_command(ctx, command[1].lower())
                else:
                    await self._command(
                        ctx, command[1].lower(), (command[3] or "").strip()
                    )
            elif (
                not ctx.group_id
                and (search := self.searches.get(ctx.actor)) is not None
                and search.awaiting_input
            ):
                if self.now() > search.expires:
                    self.searches.pop(ctx.actor, None)
                    await ctx.reply("جست‌وجو منقضی شد. با /add دوباره شروع کنید.")
                else:
                    await self._command(ctx, "add", ctx.message_text.strip())
            elif self.admin and ctx.scope in self.admin.drafts:
                await self.admin.message(ctx)
            elif not ctx.group_id:
                await self._flow_message(ctx)
        except PermissionError:
            self.logger.warning("Telegram command authorization failed")
        except Exception:
            self.logger.error("Telegram command failed")
            try:
                await ctx.reply(
                    "دریافت اطلاعات یا اجرای دستور ناموفق بود. کمی بعد دوباره تلاش کنید."
                )
            except Exception:
                self.logger.error("Telegram error reply failed")

    async def initialize(self) -> None:
        await self.api.initialize()
        if self.channels and self.channels.channel_id:
            try:
                await self.channels.refresh()
            except Exception:
                self.logger.warning("Channel startup verification unavailable")
        commands = [
            ("start", "شروع"),
            ("help", "راهنما"),
            ("add", "افزودن نماد"),
            ("list", "فهرست شخصی نمادها"),
            ("queue", "بررسی صف"),
            ("alert", "ساخت هشدار"),
            ("alerts", "مدیریت هشدارها"),
            ("history", "سابقهٔ هشدارها"),
            ("remove", "حذف نماد یا هشدار"),
            ("status", "وضعیت پایش"),
            ("cancel", "لغو فرایند"),
        ]
        await self.api.call(
            "setMyCommands",
            dict(
                commands=[dict(command=c, description=d) for c, d in commands],
                scope=dict(type="all_private_chats"),
            ),
        )
        if self.channels:
            for actor in self.channels.admins:
                if self.channels.is_admin(actor):
                    try:
                        await self.api.call(
                            "setMyCommands",
                            dict(
                                commands=[
                                    dict(command=c, description=d)
                                    for c, d in [
                                        *commands,
                                        ("admin", "مدیریت هشدارهای مقصد مشترک"),
                                    ]
                                ],
                                scope=dict(type="chat", chat_id=actor),
                            ),
                        )
                    except TelegramError:
                        self.logger.warning("Admin command hints unavailable")

    async def run(self, on_initialized: Callable[[], None] | None = None) -> None:
        if self._task is not None:
            raise RuntimeError("Telegram update consumer is already running")
        self._task = asyncio.current_task()
        try:
            await self.initialize()
            if on_initialized is not None:
                on_initialized()
            while True:
                try:
                    updates = await self.api.call(
                        "getUpdates",
                        dict(
                            offset=self.offset,
                            timeout=30,
                            limit=100,
                            allowed_updates=["message", "callback_query"],
                        ),
                        timeout=35,
                    )
                    if not isinstance(updates, list) or len(updates) > 100:
                        raise ValueError("Invalid Telegram update batch")
                    batch = [(integer(obj(u)["update_id"]), u) for u in updates]
                    if len({i for i, _ in batch}) != len(batch):
                        raise ValueError("Duplicate update IDs")
                    self.last_telegram_success, self.telegram_errors = self.now(), 0
                    for update_id, update in sorted(batch, key=lambda pair: pair[0]):
                        if update_id < self.offset:
                            continue
                        await self.handle_update(update)
                        self.offset = update_id + 1
                except TelegramError as error:
                    self.telegram_errors += 1
                    if 300 <= error.code < 500 and error.code != 429:
                        raise
                    self.logger.warning("Telegram connection unavailable")
                    await asyncio.sleep(
                        error.retry_after
                        if error.retry_after is not None
                        else min(30, 2 ** min(self.telegram_errors - 1, 5))
                    )
        finally:
            self._task = None
            self.flows.clear()
            self.searches.clear()
            if self.admin:
                self.admin.drafts.clear()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.flows.clear()
        self.searches.clear()
        if self.admin:
            self.admin.drafts.clear()

    async def _command(self, ctx: Context, command: str, argument: str) -> None:
        if command in ("start", "help"):
            help_text = (
                "نمادها و هشدارهای خصوصی خود را مدیریت کنید.\n" + LIVE_NOTICE
                if command == "start"
                else HELP + "\n" + LIVE_NOTICE
            )
            if self.channels and self.channels.is_admin(ctx.actor):
                help_text += "\n/admin مدیریت هشدارهای مقصد مشترک"
            actions = [
                ("افزودن نماد", "nav:add"),
                ("ساخت هشدار", "nav:alert"),
                ("هشدارهای من", "nav:alerts"),
                ("راهنما", "help"),
                ("تنظیم اعلان‌ها", "preferences"),
            ]
            if ctx.actor in self.flows or (
                self.admin and ctx.scope in self.admin.drafts
            ):
                help_text += "\nپیش‌نویس شما حفظ شده است؛ برای ادامه دکمهٔ زیر را بزنید."
                actions.insert(0, ("ادامهٔ پیش‌نویس", "nav:resume"))
            await ctx.reply(help_text, actions)
        elif command == "cancel":
            await ctx.reply("فرایند لغو شد.")
        elif command == "status":
            await ctx.reply(
                monitoring_text(self.get_status(), self.now()),
                [("ادامهٔ پیش‌نویس", "nav:resume")]
                if ctx.actor in self.flows
                or (self.admin and ctx.scope in self.admin.drafts)
                else None,
            )
        elif command == "admin" and self.admin:
            if self.channels and self.channels.is_admin(ctx.actor):
                await self.admin.menu(ctx)
            else:
                await ctx.reply("دسترسی مدیریت مقصد مشترک ندارید.")
        elif command == "add":
            if not argument or len(argument) > 80:
                self.searches[ctx.actor] = Search(
                    self.now() + 300_000, awaiting_input=True
                )
                await ctx.reply(
                    "نام نماد را بنویسید؛ مثلاً خودرو. می‌توانید /add خودرو هم بفرستید."
                )
                return
            search = Search(self.now() + 300_000)
            self.searches[ctx.actor] = search
            results = (await self.market.search(argument))[:8]
            if (
                self.searches.get(ctx.actor) is not search
                or self.now() > search.expires
            ):
                return
            search.instruments = results
            await ctx.reply(
                "نماد را برای افزودن انتخاب کنید؛ حداکثر هشت نتیجه نمایش داده می‌شود:"
                if results
                else "نمادی با این نام پیدا نشد.",
                [(f"{i.symbol} ({i.ins_code})", f"add:{i.ins_code}") for i in results],
            )
        elif command == "list":
            await self._symbols(ctx)
        elif command in ("queue", "remove"):
            if command == "remove" and re.fullmatch(r"rule [a-fA-F0-9-]{36}", argument):
                await self._rule_action(ctx, "delete", argument.split()[1])
                return
            matches = [
                s
                for s in self.users.list_symbols(ctx.actor)
                if s["ins_code"] == argument
                or normalize_persian(s["symbol"]) == normalize_persian(argument)
            ]
            if not argument or len(matches) > 1:
                await self._symbols(ctx, action=command)
            elif matches:
                await self._symbol_action(ctx, command, matches[0]["ins_code"])
            else:
                await ctx.reply("این نماد در فهرست شما نیست.")
        elif command == "alert":
            flow = PrivateDraft(self.now() + 600_000)
            self.flows[ctx.actor] = flow
            await self._show_flow(
                ctx, flow, notice=LIVE_NOTICE + "\nپیش‌نویس تا ۱۰ دقیقه معتبر است."
            )
        elif command == "alerts":
            await self._rules(ctx)
        elif command == "history":
            events = self.alerts.history(ctx.actor)
            for event in events:
                await ctx.reply(history_text(event))
            if not events:
                await ctx.reply("هنوز هشداری ثبت نشده است.")
        else:
            await ctx.reply(
                "دستور شناخته نشد. از /help یا منوی شروع استفاده کنید.",
                [("شروع", "nav:start")],
            )

    async def _group_command(self, ctx: Context, command: str) -> None:
        assert self.channels is not None and self.admin is not None
        self.channels.authorize(ctx.actor)
        if command in ("start", "help"):
            await ctx.reply(
                "دستورهای گروه:\n/admin مدیریت هشدارهای گروه\n/alert ساخت هشدار گروه\n/alerts فهرست هشدارهای گروه\n/status وضعیت کلی پایش\n/cancel لغو فرایند\nبرای ورود متن مراحل، به پیام همان مرحله پاسخ بدهید. دستورهای شخصی را در خصوصی اجرا کنید."
                + "\n"
                + LIVE_NOTICE
            )
        elif command == "admin":
            await self.admin.menu(ctx)
        elif command == "alert":
            await self.admin.new(ctx)
        elif command == "alerts":
            await self.admin.list(ctx)
        elif command == "status":
            await ctx.reply(monitoring_text(self.get_status(), self.now()))
        elif command == "cancel":
            await ctx.reply("فرایند گروه لغو شد.")
        else:
            await ctx.reply(
                "برای مدیریت گروه /admin را بفرستید. دستورهای شخصی فقط در خصوصی اجرا می‌شوند."
            )

    async def _preferences(self, ctx: Context) -> None:
        enabled = bool(
            (self.users.preferences(ctx.actor) or {}).get("notifications_enabled")
        )
        await ctx.reply(
            f"ارسال هشدار خصوصی: {'روشن' if enabled else 'خاموش'}\nاین تنظیم فقط ارسال خصوصی را کنترل می‌کند؛ روی هشدارهای گروه یا کانال اثر ندارد.\n{LIVE_NOTICE}",
            [
                (
                    "خاموش کردن" if enabled else "روشن کردن",
                    f"notifications:{'off' if enabled else 'on'}",
                ),
                ("بازگشت به شروع", "nav:start"),
            ],
        )

    async def _symbols(
        self, ctx: Context, page: int = 0, action: str = "queue"
    ) -> None:
        items = self.users.list_symbols(ctx.actor)
        if not items:
            await ctx.reply("فهرست شما خالی است. با /add نماد اضافه کنید.")
            return
        page = max(0, min(page, (len(items) - 1) // 10))
        buttons = [
            (i["symbol"], f"{action}:{i['ins_code']}")
            for i in items[page * 10 : page * 10 + 10]
        ]
        if page:
            buttons.append(("قبلی", f"list:{action}:{page - 1}"))
        if (page + 1) * 10 < len(items):
            buttons.append(("بعدی", f"list:{action}:{page + 1}"))
        await ctx.reply(
            "نماد را برای حذف انتخاب کنید:"
            if action == "remove"
            else "نمادهای شما؛ برای بررسی صف انتخاب کنید:",
            buttons,
        )

    async def _symbol_action(self, ctx: Context, action: str, ins_code: str) -> None:
        item = self.users.find_symbol(ctx.actor, ins_code)
        if item is None:
            await ctx.reply("این نماد در فهرست شما نیست.")
        elif action == "remove":
            await ctx.reply(
                f"نماد «{item['symbol']}» و هشدارهای خصوصی مربوط به آن حذف شوند؟ سابقهٔ این هشدارها هم حذف می‌شود. این کار قابل‌بازگشت نیست.",
                [
                    ("حذف نماد و هشدارها", f"removeconfirm:{ins_code}"),
                    ("انصراف", "nav:list"),
                ],
            )
        elif action == "removeconfirm":
            self.users.remove_symbol(ctx.actor, ins_code)
            await ctx.reply(
                "نماد و هشدارهای خصوصی آن از فهرست شما حذف شد.",
                [("نمادهای من", "nav:list")],
            )
        else:
            detection = await self.market.queue(
                Instrument(ins_code, item["symbol"], item["symbol"])
            )
            if not self.users.find_symbol(ctx.actor, ins_code):
                return
            if not detection.valid or detection.snapshot is None:
                await ctx.reply(
                    f"نماد: {item['symbol']}\nاطلاعات صف قابل تأیید نیست؛ حجم صف اعلام نمی‌شود."
                )
            else:
                value = detection.snapshot
                label = {"buy": "صف خرید", "sell": "صف فروش", "none": "بدون صف"}[
                    value.queue_type
                ]
                await ctx.reply(
                    f"نماد: {value.symbol}\nوضعیت: {label}\nحجم صف: {fa(value.volume)} سهم\nتعداد سفارش: {fa(value.order_count)}\nزمان: {tehran_time(value.timestamp)} به وقت تهران"
                )

    async def _rules(self, ctx: Context, page: int = 0) -> None:
        rules = self.alerts.list(ctx.actor)
        page = max(0, min(page, max(0, (len(rules) - 1) // 10)))
        buttons: Buttons = []
        for rule in rules[page * 10 : page * 10 + 10]:
            symbol = self.users.find_symbol(ctx.actor, rule["ins_code"])
            buttons.append(
                (
                    f"{'روشن' if rule['enabled'] else 'خاموش'} · {symbol['symbol'] if symbol else rule['ins_code']} · {LABELS[rule['definition']['type']]}",
                    f"ar:view:{rule['id']}",
                )
            )
        if page:
            buttons.append(("قبلی", f"ar:page:{page - 1}"))
        if (page + 1) * 10 < len(rules):
            buttons.append(("بعدی", f"ar:page:{page + 1}"))
        await ctx.reply(
            "هشدارهای شما؛ برای مدیریت انتخاب کنید:"
            if rules
            else "هشداری ندارید. با /alert هشدار بسازید.",
            buttons,
        )

    async def _callback(self, ctx: Context) -> None:
        data = ctx.data
        if data == "nav:resume":
            self.searches.pop(ctx.actor, None)
            if self.admin and ctx.scope in self.admin.drafts:
                draft = self.admin.drafts[ctx.scope]
                if self.admin._live(ctx.scope, draft):
                    await self.admin.show(ctx, draft)
                else:
                    self.admin.drafts.pop(ctx.scope, None)
                    await ctx.reply("پیش‌نویس منقضی شد. با /admin دوباره شروع کنید.")
            elif ctx.actor in self.flows:
                flow = self.flows[ctx.actor]
                if self.now() <= flow.expires:
                    await self._show_flow(ctx, flow)
                else:
                    self.flows.pop(ctx.actor, None)
                    await ctx.reply(EXPIRED)
            else:
                await ctx.reply("پیش‌نویسی ندارید. با /alert شروع کنید.")
        elif data in ("nav:start", "nav:add", "nav:alert", "nav:alerts", "nav:list"):
            command = data[4:]
            if command == "alert":
                self.searches.pop(ctx.actor, None)
                replaced = self.flows.pop(ctx.actor, None) is not None
                if self.admin:
                    replaced = (
                        self.admin.drafts.pop(ctx.scope, None) is not None or replaced
                    )
                if replaced:
                    await ctx.reply(
                        "پیش‌نویس قبلی کنار گذاشته شد؛ فرایند جدید شروع می‌شود."
                    )
            await self._command(ctx, command, "")
        elif data == "help":
            await self._command(ctx, "help", "")
        elif data == "preferences":
            await self._preferences(ctx)
        elif data in ("notifications:on", "notifications:off"):
            self.users.set_notifications(ctx.actor, data.endswith(":on"))
            await self._preferences(ctx)
        elif match := re.fullmatch(r"add:([1-9][0-9]{0,19})", data):
            search = self.searches.get(ctx.actor)
            item = (
                next((i for i in search.instruments if i.ins_code == match[1]), None)
                if search and self.now() <= search.expires
                else None
            )
            if item:
                self.users.add_symbol(
                    ctx.actor,
                    dict(insCode=item.ins_code, symbol=item.symbol, name=item.name),
                )
            await ctx.reply(
                f"نماد {item.symbol} به فهرست شما اضافه شد."
                if item
                else "این انتخاب منقضی شده است. جست‌وجو را با /add تکرار کنید.",
                [
                    ("ساخت هشدار برای این نماد", f"newalert:{item.ins_code}"),
                    ("نمادهای من", "nav:list"),
                ]
                if item
                else None,
            )
        elif match := re.fullmatch(r"newalert:([1-9][0-9]{0,19})", data):
            if not self.users.find_symbol(ctx.actor, match[1]):
                await ctx.reply("این نماد در فهرست شما نیست.")
                return
            replaced = self.flows.pop(ctx.actor, None) is not None
            self.searches.pop(ctx.actor, None)
            if self.admin:
                replaced = (
                    self.admin.drafts.pop(ctx.scope, None) is not None or replaced
                )
            new_flow = PrivateDraft(
                self.now() + 600_000, step="type", ins_code=match[1]
            )
            self.flows[ctx.actor] = new_flow
            await self._show_flow(
                ctx,
                new_flow,
                notice=("پیش‌نویس قبلی کنار گذاشته شد.\n" if replaced else "")
                + LIVE_NOTICE
                + "\nپیش‌نویس تا ۱۰ دقیقه معتبر است.",
            )
        elif match := re.fullmatch(r"list:(queue|remove):([0-9]+)", data):
            await self._symbols(ctx, int(match[2]), match[1])
        elif match := re.fullmatch(
            r"(queue|remove|removeconfirm):([1-9][0-9]{0,19})", data
        ):
            await self._symbol_action(ctx, match[1], match[2])
        elif data == "remove:alerts":
            await self._rules(ctx)
        elif match := re.fullmatch(
            r"ar:(view|on|off|edit|delete|confirmdelete|page):([a-f0-9-]+)", data
        ):
            if match[1] == "page" and match[2].isdigit():
                await self._rules(ctx, int(match[2]))
            else:
                await self._rule_action(ctx, match[1], match[2])
        elif match := re.fullmatch(r"aw:([a-f0-9]{8}):(.+)", data):
            await self._flow_callback(ctx, match[1], match[2])

    async def _rule_action(self, ctx: Context, action: str, rule_id: str) -> None:
        rule = self.alerts.get(ctx.actor, rule_id)
        if rule is None:
            await ctx.reply("این هشدار در فهرست شما نیست.")
        elif action == "confirmdelete":
            self.alerts.remove(ctx.actor, rule_id)
            await ctx.reply("هشدار حذف شد.")
        elif action == "delete":
            await ctx.reply(
                "این هشدار و سابقهٔ آن حذف شود؟",
                [
                    ("تأیید حذف", f"ar:confirmdelete:{rule_id}"),
                    ("بازگشت", f"ar:view:{rule_id}"),
                ],
            )
        elif action in ("on", "off"):
            self.alerts.set_enabled(ctx.actor, rule_id, action == "on")
            symbol = self.users.find_symbol(ctx.actor, rule["ins_code"])
            await ctx.reply(
                f"هشدار «{symbol['symbol'] if symbol else rule['ins_code']}» {'روشن' if action == 'on' else 'خاموش'} شد.",
                [("مشاهدهٔ هشدار", f"ar:view:{rule_id}"), ("هشدارهای من", "nav:alerts")],
            )
        elif action == "edit":
            self.searches.pop(ctx.actor, None)
            if self.admin and self.admin.drafts.pop(ctx.scope, None) is not None:
                await ctx.reply(
                    "پیش‌نویس مقصد مشترک کنار گذاشته شد؛ ویرایش هشدار خصوصی شروع می‌شود."
                )
            flow = PrivateDraft(
                self.now() + 600_000,
                step="type",
                ins_code=rule["ins_code"],
                rule_id=rule_id,
                revision=rule["revision"],
                kind=rule["definition"]["type"],
                window=rule["definition"]["config"].get("windowMinutes"),
                percent=rule["definition"]["config"].get("thresholdPercent"),
                minimum=rule["definition"]["config"].get(
                    "minimumBaselineVolume",
                    rule["definition"]["config"].get("minimumPreviousVolume"),
                ),
            )
            self.flows[ctx.actor] = flow
            await self._show_flow(ctx, flow)
        elif action == "view":
            symbol = self.users.find_symbol(ctx.actor, rule["ins_code"])
            await ctx.reply(
                f"نماد: {symbol['symbol'] if symbol else rule['ins_code']}\n{tree_text(parse_tree(rule['definition'], channel=False))}\nوضعیت: {'روشن' if rule['enabled'] else 'خاموش'}\nوقفهٔ هشدار: {fa(rule['cooldown_seconds'])} ثانیه\nشناسه: {rule_id}",
                [
                    (
                        "خاموش کردن" if rule["enabled"] else "روشن کردن",
                        f"ar:{'off' if rule['enabled'] else 'on'}:{rule_id}",
                    ),
                    ("ویرایش تنظیمات", f"ar:edit:{rule_id}"),
                    ("حذف", f"ar:delete:{rule_id}"),
                    ("بازگشت به هشدارها", "nav:alerts"),
                ],
            )

    async def _show_flow(
        self, ctx: Context, flow: PrivateDraft, page: int = 0, notice: str = ""
    ) -> None:
        flow.token = secrets.token_hex(4)
        actions: Buttons = []
        prompt = ""
        if flow.step == "symbol":
            watched = self.users.list_symbols(ctx.actor)
            if not watched:
                self.flows.pop(ctx.actor, None)
                await ctx.reply("ابتدا با /add نماد را به فهرست خود اضافه کنید.")
                return
            page = max(0, min(page, (len(watched) - 1) // 10))
            prompt = "ساخت هشدار جدید؛ نماد را انتخاب کنید:"
            actions = [
                (s["symbol"], f"symbol{s['ins_code']}")
                for s in watched[page * 10 : page * 10 + 10]
            ]
            if page:
                actions.append(("قبلی", f"page{page - 1}"))
            if (page + 1) * 10 < len(watched):
                actions.append(("بعدی", f"page{page + 1}"))
        elif flow.step == "type":
            prompt = "نوع هشدار را انتخاب کنید:"
            actions = [(LABELS[t], f"type{i}") for i, t in enumerate(PRIVATE_TYPES)]
        elif flow.step == "window":
            prompt, actions = (
                "بازهٔ مقایسه را انتخاب کنید:",
                [("۱ دقیقه", "window1"), ("۵ دقیقه", "window5")],
            )
        elif flow.step == "threshold":
            prompt = "درصد کاهش را بنویسید؛ بزرگ‌تر از صفر تا ۱۰۰، حداکثر شش رقم اعشار."
        elif flow.step == "minimum":
            prompt = minimum_prompt(flow.kind)
        elif flow.step == "confirm":
            symbol = self.users.find_symbol(ctx.actor, flow.ins_code)
            if not symbol:
                self.flows.pop(ctx.actor, None)
                await ctx.reply("این نماد در فهرست شما نیست.")
                return
            prompt = f"تأیید هشدار\nنماد: {symbol['symbol']}\n{tree_text(parse_tree(flow.definition(), channel=False))}"
            actions = [("تأیید و ذخیره", "save")]
        current = {
            "threshold": flow.percent,
            "window": flow.window,
            "minimum": flow.minimum,
        }.get(flow.step)
        if current is not None:
            prompt += f"\nمقدار فعلی: {fa(current)}" + (
                "٪"
                if flow.step == "threshold"
                else " دقیقه"
                if flow.step == "window"
                else " سهم"
            )
            actions.append(("نگه‌داشتن مقدار فعلی", "keep"))
        if flow.step == "type" and flow.kind:
            prompt += f"\nنوع فعلی: {LABELS[flow.kind]}"
            actions.append(("نگه‌داشتن نوع فعلی", "keep"))
        if flow.step != "symbol" and not (flow.step == "type" and flow.rule_id):
            actions.append(("بازگشت", "back"))
        actions.append(("لغو", "cancel"))
        await ctx.reply(
            (notice + "\n" if notice else "") + prompt,
            [(label, f"aw:{flow.token}:{action}") for label, action in actions],
            edit=True,
        )

    async def _flow_callback(self, ctx: Context, token: str, action: str) -> None:
        flow = self.flows.get(ctx.actor)
        if flow is None or flow.token != token or self.now() > flow.expires:
            if flow and self.now() > flow.expires:
                self.flows.pop(ctx.actor, None)
            await ctx.reply(EXPIRED)
            return
        if action == "cancel":
            self.flows.pop(ctx.actor, None)
            await ctx.reply("ساخت هشدار لغو شد.")
            return
        if action == "back":
            flow.step = {
                "symbol": "symbol",
                "type": "symbol" if not flow.rule_id else "type",
                "window": "type",
                "threshold": "window",
                "minimum": "threshold"
                if flow.kind.endswith("DROP_PERCENT")
                else "type",
                "confirm": "minimum",
            }[flow.step]
        elif action == "keep" and flow.step == "type" and flow.kind:
            flow.step = "window" if flow.kind.endswith("DROP_PERCENT") else "minimum"
        elif action == "keep" and flow.step == "window" and flow.window is not None:
            flow.step = "threshold"
        elif action == "keep" and flow.step == "threshold" and flow.percent is not None:
            flow.step = "minimum"
        elif action == "keep" and flow.step == "minimum" and flow.minimum is not None:
            flow.step = "confirm"
        elif flow.step == "symbol" and re.fullmatch(r"page[0-9]+", action):
            await self._show_flow(ctx, flow, int(action[4:]))
            return
        elif flow.step == "symbol" and re.fullmatch(r"symbol[1-9][0-9]{0,19}", action):
            if not self.users.find_symbol(ctx.actor, action[6:]):
                await ctx.reply("این نماد در فهرست شما نیست.")
                return
            flow.ins_code, flow.step = action[6:], "type"
        elif flow.step == "type" and re.fullmatch(r"type[0-3]", action):
            flow.kind = PRIVATE_TYPES[int(action[4:])]
            flow.percent, flow.window, flow.minimum = None, None, None
            flow.step = "window" if flow.kind.endswith("DROP_PERCENT") else "minimum"
        elif flow.step == "window" and action in ("window1", "window5"):
            flow.window, flow.step = int(action[-1]), "threshold"
        elif flow.step == "confirm" and action == "save":
            definition = flow.definition()
            if flow.rule_id:
                rule = self.alerts.get(ctx.actor, flow.rule_id)
                if (
                    not rule
                    or rule["revision"] != flow.revision
                    or not self.alerts.update(ctx.actor, flow.rule_id, definition)
                ):
                    self.flows.pop(ctx.actor, None)
                    await ctx.reply(
                        "هشدار تغییر کرده یا حذف شده است. از /alerts دوباره انتخاب کنید."
                    )
                    return
            else:
                self.alerts.create(ctx.actor, flow.ins_code, definition)
            self.flows.pop(ctx.actor, None)
            await ctx.reply(
                "هشدار ذخیره شد.\n" + LIVE_NOTICE,
                [("هشدارهای من", "nav:alerts"), ("ساخت هشدار دیگر", "nav:alert")],
            )
            return
        else:
            await ctx.reply(EXPIRED)
            return
        await self._show_flow(ctx, flow)

    async def _flow_message(self, ctx: Context) -> None:
        flow = self.flows.get(ctx.actor)
        if flow is None:
            return
        if self.now() > flow.expires:
            self.flows.pop(ctx.actor, None)
            await ctx.reply(EXPIRED)
            return
        try:
            value = numeric(ctx.message_text)
            if flow.step == "threshold":
                if not 0 < value <= 100 or round(value, 6) != value:
                    raise ValueError("Invalid percentage")
                flow.percent, flow.step = value, "minimum"
            elif flow.step == "minimum":
                flow.minimum, flow.step = integer(value), "confirm"
            else:
                await self._show_flow(
                    ctx, flow, notice="از دکمه‌های آخرین پیام استفاده کنید."
                )
                return
        except ValueError, OverflowError:
            await self._show_flow(
                ctx,
                flow,
                notice=numeric_error(flow.step, flow.kind),
            )
            return
        await self._show_flow(ctx, flow)
