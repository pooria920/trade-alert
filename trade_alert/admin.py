"""Current-admin shared-destination UI, with per-chat expiring flat-tree drafts."""

import asyncio
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from .alerts import AlertService
from .bot_text import (
    ADMIN_TYPES,
    HYSTERESIS_PROMPT,
    LABELS,
    LIFECYCLE_LABELS,
    LIVE_NOTICE,
    PERCENT_PROMPT,
    PERMISSION_NOTICES,
    TYPE_PROMPT,
    WINDOW_PROMPT,
    fa,
    first_step,
    history_text,
    minimum_prompt,
    numeric,
    numeric_error,
    statistics_text,
    tehran_time,
    tree_text,
    type_buttons,
    wizard_config,
)
from .channels import ChannelService
from .database import rows
from .definitions import definition_json, parse_tree
from .lifecycle import LifecycleTracker
from .market import Instrument
from .market_service import MarketService
from .quality import SEVERITIES, SEVERITY_LABELS, parse_policy, stored_policy
from .telegram import Buttons, Context
from .validation import integer, text


@dataclass(slots=True)
class ChannelDraft:
    expires: int
    channel_id: str
    token: str = ""
    step: str = "channel"
    operator: str = "AND"
    conditions: list[dict[str, Any]] = field(default_factory=list)
    instrument: Instrument | None = None
    results: list[Instrument] = field(default_factory=list)
    selected_type: str = ""
    percent: float | None = None
    window: int | None = None
    minimum: int | None = None
    edit_index: int | None = None
    rule_id: str | None = None
    revision: int | None = None
    saving: bool = False
    message_id: int | None = None
    hysteresis: float = 5
    legacy_volume: bool = False
    family: str = ""
    disappearance_seconds: int = 0

    def tree(self) -> dict[str, Any]:
        return definition_json(
            parse_tree(
                dict(type="GROUP", operator=self.operator, conditions=self.conditions)
            )
        )


@dataclass(slots=True)
class MessageDraft:
    expires: int
    channel_id: str
    token: str = field(default_factory=lambda: secrets.token_hex(4))
    instrument: Instrument | None = None
    body: str = ""
    preview: bool = False
    message_id: int | None = None


@dataclass(slots=True)
class QualityDraft:
    expires: int
    channel_id: str
    private: bool
    rule: dict[str, Any]
    settings: dict[str, Any]
    cooldown: int
    token: str = ""
    field: str = ""
    saving: bool = False


class AdminCommands:
    def __init__(
        self,
        channels: ChannelService,
        market: MarketService,
        now: Callable[[], int],
        alerts: AlertService | None = None,
        *,
        lifecycle: LifecycleTracker | None = None,
        is_market_open: Callable[[], bool] = lambda: False,
        session_at: Callable[[int], str | None] = lambda timestamp: None,
    ) -> None:
        self.channels, self.market, self.now = channels, market, now
        # ponytail: one ten-minute draft per admin/chat (private + one group), lost on restart.
        self.drafts: dict[str, ChannelDraft] = {}
        self.messages: dict[str, MessageDraft] = {}
        self.settings: dict[str, QualityDraft] = {}
        self.alerts = alerts
        self.lifecycle = lifecycle or LifecycleTracker(channels.db)
        self.is_market_open = is_market_open
        self.session_at = session_at

    def discard(self, scope: str) -> bool:
        rule = self.drafts.pop(scope, None)
        message = self.messages.pop(scope, None)
        quality = self.settings.pop(scope, None)
        return rule is not None or message is not None or quality is not None

    def _live(self, scope: str, draft: ChannelDraft) -> bool:
        return (
            self.drafts.get(scope) is draft
            and self.now() <= draft.expires
            and draft.channel_id == self.channels.channel_id
        )

    async def menu(self, ctx: Context) -> None:
        self.channels.authorize(ctx.actor)
        self.drafts.pop(ctx.scope, None)
        self.messages.pop(ctx.scope, None)
        self.settings.pop(ctx.scope, None)
        await ctx.reply(
            ("مدیریت هشدارهای گروه" if ctx.group_id else "مدیریت هشدارهای مقصد مشترک")
            + f"\nمقصد: {self.channels.status(ctx.actor)['title']}\n{LIVE_NOTICE}",
            [
                ("ساخت هشدار جدید", "ad:new"),
                ("مشاهده هشدارها", "ad:list:0"),
                ("تاریخچه و نتیجهٔ هشدارها", "ad:history:0"),
                ("آمار ۳۰ روز اخیر", "ad:stats"),
                ("رفتار مشاهده‌شدهٔ صف", "ad:life:page:0"),
                ("وضعیت گروه" if ctx.group_id else "وضعیت مقصد مشترک", "ad:status"),
            ]
            + ([] if ctx.group_id else [("ارسال پیام به گروه یا کانال", "ad:post")]),
            edit=True,
        )

    async def show(self, ctx: Context, draft: ChannelDraft, notice: str = "") -> None:
        self.channels.authorize(ctx.actor)
        draft.token = secrets.token_hex(4)
        destination = "گروه" if ctx.group_id else "مقصد مشترک"
        actions: Buttons = []
        prompt = ""
        if draft.step == "channel":
            prompt = f"هشدار جدید {destination}\n{destination}: {self.channels.status(ctx.actor)['title']}\nپیش‌نویس تا ۱۰ دقیقه معتبر است.\n{LIVE_NOTICE}"
            actions = [(f"انتخاب این {destination}", "channel")]
        elif draft.step == "search":
            prompt = "نام نماد بورسی را برای جست‌وجو بنویسید؛ مثلاً «خودرو» یا نماد دلخواهتان. حداکثر ۸۰ نویسه."
        elif draft.step == "results":
            prompt = "نماد را انتخاب کنید:"
            actions = [
                (item.symbol, f"symbol{i}") for i, item in enumerate(draft.results)
            ]
        elif draft.step == "type":
            prompt = (
                TYPE_PROMPT
                + "\n«حداقل حجم صف» یعنی حجم سفارش‌های صف به تعداد سهم تعیین‌شده برسد؛ نه حجم معاملات."
            )
            actions = type_buttons(draft.family, channel=True)
            if draft.family in ("life", "formed", "weakened", "gone", "reformed"):
                prompt = f"هشدارهای رفتار صف\nچه اتفاقی را بررسی کنیم؟\nتأیید تغییر پس از {fa(self.lifecycle.options.confirmation_snapshots)} مشاهدهٔ تازهٔ پیاپی؛ وقفهٔ داده رویداد بازار نیست."
        elif draft.step == "percent":
            prompt = PERCENT_PROMPT
            actions = [(fa(n) + "٪", f"percent{n}") for n in (10, 20, 30, 50, 75)]
            if draft.selected_type in LIFECYCLE_LABELS:
                actions.append(("مقدار دلخواه", "custom"))
        elif draft.step == "hysteresis":
            prompt = HYSTERESIS_PROMPT
        elif draft.step == "window":
            prompt, actions = (
                WINDOW_PROMPT,
                [
                    ("۱ دقیقه", "window1"),
                    ("۵ دقیقه", "window5"),
                    ("۱۵ دقیقه", "window15"),
                ],
            )
        elif draft.step == "minimum":
            prompt = minimum_prompt(draft.selected_type)
            if draft.selected_type in LIFECYCLE_LABELS:
                actions = [
                    (fa(v // 1_000_000) + " میلیون", f"volume{v}")
                    for v in (1_000_000, 5_000_000, 10_000_000)
                ] + [("مقدار دلخواه", "custom")]
            if draft.selected_type.endswith("REFORMED"):
                actions.append(("تنظیم فاصله تا تشکیل دوباره", "duration"))
            if (
                draft.selected_type.endswith(
                    ("VOLUME_BELOW", "VOLUME_ABOVE", "PEAK_DROP_PERCENT")
                )
                and not draft.legacy_volume
            ):
                actions.append(("تنظیم فاصلهٔ بازگشت", "hysteresis"))
        elif draft.step == "duration":
            prompt = "حداقل چند ثانیه پس از جمع شدن تأییدشده، تشکیل دوباره بررسی شود؟ عددی از ۰ تا ۸۶۴۰۰ بنویسید. پیش‌فرض ۰ است."
        elif draft.step == "builder":
            detail = (
                tree_text(parse_tree(draft.tree()))
                if draft.conditions
                else "هنوز شرطی ثبت نشده است."
            )
            prompt = f"هشدار {destination}\n{destination}: {self.channels.status(ctx.actor)['title']}\nنماد: {draft.instrument.symbol if draft.instrument else ''}\n{detail}\n{LIVE_NOTICE}"
            for i in range(len(draft.conditions)):
                actions.extend(
                    [
                        (f"ویرایش شرط {fa(i + 1)}", f"edit{i}"),
                        (f"حذف شرط {fa(i + 1)}", f"remove{i}"),
                    ]
                )
            if len(draft.conditions) < 8:
                actions.append(("افزودن شرط", "add"))
            actions.append(
                (
                    "تغییر به: حداقل یکی از شرط‌ها"
                    if draft.operator == "AND"
                    else "تغییر به: همهٔ شرط‌ها",
                    "operator",
                )
            )
            if draft.conditions:
                actions.append(("تأیید و ذخیره", "save"))
        current = {
            "percent": draft.percent,
            "window": draft.window,
            "minimum": draft.minimum,
        }.get(draft.step)
        if current is not None:
            prompt += f"\nمقدار فعلی: {fa(current)}" + (
                "٪"
                if draft.step == "percent"
                else " دقیقه"
                if draft.step == "window"
                else " سهم"
            )
            actions.append(("نگه‌داشتن مقدار فعلی", "keep"))
        if draft.step == "type" and draft.selected_type:
            prompt += f"\nنوع فعلی: {LABELS[draft.selected_type]}"
            actions.append(("نگه‌داشتن نوع فعلی", "keep"))
        actions.extend([("بازگشت", "back"), ("لغو", "cancel")])
        if draft.instrument:
            note = self.channels.symbol_note(draft.instrument.ins_code)
            prompt += "\n\nیادداشت مشترک نماد: " + (note or "ثبت نشده")
            actions.append(("ویرایش یادداشت نماد", "note"))
        if ctx.group_id:
            prompt += "\nبرای ورود متن، به همین پیام پاسخ بدهید."
        draft.message_id = await ctx.reply(
            (notice + "\n\n" if notice else "") + prompt,
            [(label, f"af:{draft.token}:{action}") for label, action in actions],
            edit=True,
        )

    def _lifecycle_symbols(self, actor: str) -> dict[str, str]:
        self.channels.authorize(actor)
        shared = {i.ins_code: i.symbol for i in self.channels.monitored_symbols()}
        shared.update(
            {
                r["ins_code"]: r["symbol"]
                for r in self.channels.users.list_symbols(actor)
            }
        )
        return shared

    async def lifecycle_menu(self, ctx: Context, page: int = 0) -> None:
        symbols = sorted(
            self._lifecycle_symbols(ctx.actor).items(), key=lambda item: item[1]
        )
        page = max(0, min(page, max(0, (len(symbols) - 1) // 10)))
        buttons = [
            (symbol, f"ad:life:buy:{code}")
            for code, symbol in symbols[page * 10 : page * 10 + 10]
        ]
        if page:
            buttons.append(("قبلی", f"ad:life:page:{page - 1}"))
        if (page + 1) * 10 < len(symbols):
            buttons.append(("بعدی", f"ad:life:page:{page + 1}"))
        buttons.append(("بازگشت", "ad:menu"))
        await ctx.reply(
            "رفتار مشاهده‌شدهٔ صف؛ نماد شخصی خودتان یا مقصد مشترک را انتخاب کنید:"
            if symbols
            else "نمادی در فهرست شخصی شما یا هشدارهای فعال مقصد مشترک نیست.",
            buttons,
            edit=True,
        )

    async def lifecycle_detail(self, ctx: Context, ins_code: str, side: str) -> None:
        symbols = self._lifecycle_symbols(ctx.actor)
        if ins_code not in symbols or side not in ("buy", "sell"):
            await ctx.reply("این نماد در محدودهٔ دسترسی فعلی نیست.", edit=True)
            return
        found = rows(
            self.channels.db,
            "SELECT * FROM queue_lifecycle_states WHERE ins_code=? AND side=?",
            (ins_code, side),
        )
        buttons = [
            ("صف خرید", f"ad:life:buy:{ins_code}"),
            ("صف فروش", f"ad:life:sell:{ins_code}"),
            ("بازگشت", "ad:life:page:0"),
        ]
        title = f"رفتار صف {'خرید' if side == 'buy' else 'فروش'} {symbols[ins_code]}"
        if not found:
            await ctx.reply(
                title + "\nمشاهدهٔ تأییدشده‌ای ثبت نشده است.\n" + LIVE_NOTICE,
                buttons,
                edit=True,
            )
            return
        state = found[0]
        fresh = (
            self.is_market_open()
            and self.session_at(self.now()) == state["session_id"]
            and state["observation_valid"]
            and state["continuous"]
            and not state["pending_count"]
            and 0
            <= self.now() - state["last_source_at"]
            <= min(self.lifecycle.options.max_gap_ms, self.market.detector.max_age_ms)
        )
        labels = {
            "UNKNOWN": "هنوز تأیید نشده",
            "NO_QUEUE": "بدون صف",
            "ACTIVE": "صف فعال",
            "WEAKENED": "صف ضعیف‌شده",
            "DISAPPEARED": "جمع شدن صف تأیید شده",
        }
        peaks = rows(
            self.channels.db,
            "SELECT * FROM queue_session_peaks WHERE ins_code=? AND session_id=?",
            (ins_code, state["session_id"]),
        )
        peak = peaks[0][f"{side}_volume"] if peaks else 0
        message = (
            title
            + f"\nآخرین وضعیت تأییدشده: {labels.get(state['status'], 'نامشخص')}\nمشاهدهٔ فعلی: {'معتبر و تازه' if fresh else 'در دسترس نیست یا هنوز تأیید نشده'}"
        )
        message += f"\nآخرین حجم تأییدشده: {fa(state['last_volume']) if state['last_volume'] is not None else 'موجود نیست'} سهم\nبیشترین حجم مشاهده‌شده: {fa(peak) if peak else 'موجود نیست'} سهم\nدفعات تشکیل دوباره: {fa(state['cycles'])}\nآخرین تشکیل: {tehran_time(state['last_formed_at'])}\nآخرین جمع شدن: {tehran_time(state['last_disappeared_at'])}\nآخرین مشاهده: {tehran_time(state['last_source_at'])}\nشروع پوشش مشاهده: {tehran_time(state['first_observed_at'])}"
        events = [
            e
            for e in self.lifecycle.history(ins_code, state["session_id"])
            if e["side"] == side
        ][:5]
        event_labels = {
            "QUEUE_FORMED": "تشکیل صف",
            "QUEUE_WEAKENED": "ضعیف شدن صف",
            "QUEUE_DISAPPEARED": "جمع شدن صف",
            "QUEUE_REFORMED": "تشکیل دوباره صف",
        }
        message += "\nرویدادهای اخیر همین جلسه:\n" + (
            "\n".join(
                f"{event_labels[e['event_type']]}: {tehran_time(e['timestamp'])}"
                for e in events
            )
            or "رویدادی ثبت نشده است."
        )
        message += "\nزمان آخرین حجم تأییدشده: " + tehran_time(state["last_volume_at"])
        message += "\nپوشش از ابتدای جلسه تضمین نمی‌شود. این مشاهدهٔ رفتار صف است، نه توصیهٔ معامله."
        await ctx.reply(message, buttons, edit=True)

    async def list(self, ctx: Context, page: int = 0) -> None:
        rules = self.channels.list(ctx.actor)
        destination = "گروه" if ctx.group_id else "مقصد مشترک"
        page = max(0, min(page, max(0, (len(rules) - 1) // 10)))
        buttons = [
            (
                f"{'روشن' if r['enabled'] else 'خاموش'} · {r['symbol']} · {'هشدار ترکیبی' if r['definition']['type'] == 'GROUP' else LABELS[r['definition']['type']]}",
                f"ad:view:{r['id']}",
            )
            for r in rules[page * 10 : page * 10 + 10]
        ]
        if page:
            buttons.append(("قبلی", f"ad:list:{page - 1}"))
        if (page + 1) * 10 < len(rules):
            buttons.append(("بعدی", f"ad:list:{page + 1}"))
        buttons.append(("بازگشت", "ad:menu"))
        await ctx.reply(
            f"هشدارهای {destination}؛ برای مدیریت انتخاب کنید:"
            if rules
            else f"هشداری برای این {destination} ثبت نشده است.",
            buttons,
            edit=True,
        )

    async def compose_note(self, ctx: Context, item: Instrument) -> None:
        self.channels.authorize(ctx.actor)
        if rule := self.drafts.get(ctx.scope):
            rule.token = secrets.token_hex(4)  # Pause the rule wizard's old buttons.
        self.messages[ctx.scope] = MessageDraft(
            self.now() + 600_000, self.channels.channel_id, instrument=item
        )
        await self._show_message(ctx)

    async def _show_message(self, ctx: Context, notice: str = "") -> None:
        self.channels.authorize(ctx.actor)
        draft = self.messages[ctx.scope]
        if self.now() > draft.expires or draft.channel_id != self.channels.channel_id:
            self.messages.pop(ctx.scope, None)
            await ctx.reply("پیش‌نویس منقضی شد. با /admin دوباره شروع کنید.")
            return
        draft.token = secrets.token_hex(4)
        title = (
            f"یادداشت مشترک نماد «{draft.instrument.symbol}»"
            if draft.instrument
            else "ارسال پیام به گروه یا کانال"
        )
        prompt = f"{title}\nمقصد: {self.channels.status(ctx.actor)['title']}\nپیش‌نویس تا ۱۰ دقیقه معتبر است.\n"
        actions = []
        if draft.preview:
            prompt += "پیش‌نمایش؛ هنوز ذخیره یا ارسال نشده است:\n\n" + (
                draft.body or "حذف یادداشت فعلی"
            )
            actions.extend(
                [
                    (
                        "تأیید و ذخیره" if draft.instrument else "تأیید ارسال به مقصد",
                        "save",
                    ),
                    ("ویرایش متن", "edit"),
                ]
            )
        elif draft.instrument:
            prompt += (
                "این یادداشت را مدیرها می‌بینند و همراه هشدارهای این نماد در مقصد منتشر می‌شود. حداکثر ۵۰۰ نویسه بنویسید.\nمثال: بررسی دوباره پس از انتشار گزارش ماهانه.\nیادداشت فعلی: "
                + (self.channels.symbol_note(draft.instrument.ins_code) or "ثبت نشده")
            )
            actions.append(("حذف یادداشت", "clear"))
        else:
            prompt += "متن پیام را بنویسید؛ حداکثر ۱۵۰۰ نویسه. پس از پیش‌نمایش، فقط با تأیید شما ارسال می‌شود."
        actions.append(("لغو", "cancel"))
        if ctx.group_id:
            prompt += "\nمتن را با Reply به همین پیام بفرستید."
        draft.message_id = await ctx.reply(
            (notice + "\n" if notice else "") + prompt,
            [(label, f"ad:write:{draft.token}:{action}") for label, action in actions],
            edit=True,
        )

    async def _message_callback(self, ctx: Context) -> None:
        await ctx.answer()
        self.channels.authorize(ctx.actor)
        match = re.fullmatch(
            r"ad:write:([a-f0-9]{8}):(save|edit|clear|cancel)", ctx.data
        )
        draft = self.messages.get(ctx.scope)
        if ctx.group_id and draft and ctx.message_id != draft.message_id:
            return
        if (
            not match
            or not draft
            or match[1] != draft.token
            or self.now() > draft.expires
            or draft.channel_id != self.channels.channel_id
        ):
            await ctx.reply(
                "این پیش‌نویس منقضی یا تغییر کرده است. از /admin دوباره شروع کنید."
            )
            return
        action = match[2]
        if action == "edit":
            draft.preview = False
            await self._show_message(ctx)
        elif action == "clear" and draft.instrument:
            draft.body, draft.preview = "", True
            await self._show_message(ctx)
        elif action == "cancel":
            self.messages.pop(ctx.scope, None)
            await self._resume_message(ctx, "لغو شد؛ تغییری ذخیره یا ارسال نشد.")
        elif action == "save" and draft.preview:
            # Consume before awaiting permissions/send: a repeated click cannot send twice.
            self.messages.pop(ctx.scope, None)
            if draft.instrument:
                item = draft.instrument
                self.channels.set_symbol_note(
                    ctx.actor,
                    dict(insCode=item.ins_code, symbol=item.symbol, name=item.name),
                    draft.body,
                )
                await self._resume_message(
                    ctx,
                    "یادداشت مشترک نماد ذخیره شد."
                    if draft.body
                    else "یادداشت نماد حذف شد.",
                )
            elif not ctx.group_id:
                try:
                    async with asyncio.timeout(10):
                        if not await self.channels.refresh():
                            raise PermissionError("Posting unavailable")
                        self.channels.authorize(ctx.actor)
                        if (
                            draft.channel_id != self.channels.channel_id
                            or self.now() > draft.expires
                        ):
                            raise PermissionError("Destination changed")
                        await ctx.api.call(
                            "sendMessage",
                            dict(chat_id=self.channels.channel_id, text=draft.body),
                        )
                except Exception:
                    await ctx.reply(
                        "ارسال تأیید نشد. پیش از تلاش دوباره، مقصد را بررسی کنید؛ ممکن است پیام رسیده باشد. ارسال خودکار تکرار نمی‌شود."
                    )
                    return
                await ctx.reply("پیام به مقصد تنظیم‌شده ارسال شد.")

    async def _resume_message(self, ctx: Context, notice: str) -> None:
        draft = self.drafts.get(ctx.scope)
        if draft and self._live(ctx.scope, draft):
            await self.show(ctx, draft, notice)
        else:
            await ctx.reply(notice, [("مدیریت گروه یا کانال", "ad:menu")], edit=True)

    async def status(self, ctx: Context) -> None:
        if ctx.group_id:
            await ctx.reply(
                "جزئیات مجوز و خطاهای ارسال فقط در /admin خصوصی مدیر نمایش داده می‌شود."
            )
            return
        try:
            await self.channels.refresh()
        except Exception:
            pass
        value = self.channels.status(ctx.actor)
        error = value["last_error"]
        verification = PERMISSION_NOTICES.get(
            value["permission_error"],
            "بررسی مقصد موفق بود."
            if value["can_post"]
            else "مجوز فعلی تأیید نشده است؛ «بررسی دوباره» را بزنید.",
        )
        await ctx.reply(
            f"وضعیت مقصد مشترک\nعنوان: {value['title']}\nشناسهٔ مقصد: {value['channel_id']}\nاجازهٔ ارسال: {'دارد' if value['can_post'] else 'ندارد یا قابل تأیید نیست'}\nنتیجهٔ بررسی مقصد: {verification}\nتعداد هشدارها: {fa(value['total'])}\nفعال: {fa(value['active'])}\nآخرین ارسال موفق: {tehran_time(value['last_success'])}\nآخرین خطای ارسال: {error['code'] + ' · ' + tehran_time(error['timestamp']) if error else 'ثبت نشده'}",
            [("بررسی دوباره", "ad:status"), ("بازگشت", "ad:menu")],
            edit=True,
        )

    async def callback(self, ctx: Context) -> None:
        if not self.channels.is_admin(ctx.actor):
            await ctx.answer("دسترسی ندارید.")
            return
        self.channels.authorize(ctx.actor)
        if ctx.data.startswith("qf:"):
            await ctx.answer()
            await self._quality_callback(ctx)
            return
        if ctx.data.startswith("ad:write:"):
            await self._message_callback(ctx)
            return
        await ctx.answer()
        self.channels.authorize(ctx.actor)
        if ctx.data.startswith("af:"):
            match = re.fullmatch(r"af:([a-f0-9]{8}):(.+)", ctx.data)
            if match:
                await self._draft_callback(ctx, match[1], match[2])
            return
        action = ctx.data[3:]
        if action.startswith("life:"):
            match = re.fullmatch(r"life:(buy|sell|page):([0-9]+)", action)
            if match:
                if match[1] == "page":
                    await self.lifecycle_menu(ctx, integer(int(match[2]), 0, 1_000_000))
                else:
                    await self.lifecycle_detail(ctx, match[2], match[1])
            return
        if action == "menu":
            await self.menu(ctx)
        elif action == "status":
            await self.status(ctx)
        elif action == "new":
            await self.new(ctx)
        elif action == "stats":
            await ctx.reply(
                statistics_text(await self.channels.statistics(ctx.actor)),
                [("بازگشت", "ad:menu")],
                edit=True,
            )
        elif match := re.fullmatch(r"history:([0-9]{1,7})", action):
            page = int(match[1])
            events = self.channels.history(ctx.actor, page)
            for event in events:
                await ctx.reply(history_text(event))
            buttons = [("بازگشت", "ad:menu")]
            if page:
                buttons.append(("قبلی", f"ad:history:{page - 1}"))
            if len(events) == 10:
                buttons.append(("بعدی", f"ad:history:{page + 1}"))
            await ctx.reply(
                "تاریخچهٔ مقصد مشترک" if events else "رخداد دیگری ثبت نشده است.", buttons
            )
        elif match := re.fullmatch(r"quality([pc]):([a-f0-9-]{36})", action):
            private = match[1] == "p"
            rule = (
                self.alerts.get(ctx.actor, match[2])
                if private and self.alerts
                else self.channels.get(ctx.actor, match[2])
                if not private
                else None
            )
            if rule:
                self.discard(ctx.scope)
                self.settings[ctx.scope] = QualityDraft(
                    self.now() + 600_000,
                    self.channels.channel_id,
                    private,
                    rule,
                    dict(stored_policy(rule["quality_config"]).to_json()),
                    rule["cooldown_seconds"],
                )
                await self._show_quality(ctx)
        elif action == "post" and not ctx.group_id:
            self.settings.pop(ctx.scope, None)
            self.drafts.pop(ctx.scope, None)
            self.messages[ctx.scope] = MessageDraft(
                self.now() + 600_000, self.channels.channel_id
            )
            await self._show_message(ctx)
        elif match := re.fullmatch(r"note:([1-9][0-9]{0,19})", action):
            item = self.channels.users.find_symbol(ctx.actor, match[1])
            if item:
                await self.compose_note(
                    ctx, Instrument(item["ins_code"], item["symbol"], item["symbol"])
                )
        elif re.fullmatch(r"list:[0-9]+", action):
            await self.list(ctx, int(action[5:]))
        elif match := re.fullmatch(
            r"(view|edit|on|off|delete|confirmdelete):([a-f0-9-]{36})", action
        ):
            await self._rule_action(ctx, match[1], match[2])

    async def new(self, ctx: Context) -> None:
        self.channels.authorize(ctx.actor)
        self.settings.pop(ctx.scope, None)
        self.messages.pop(ctx.scope, None)
        draft = ChannelDraft(self.now() + 600_000, self.channels.channel_id)
        self.drafts[ctx.scope] = draft
        await self.show(ctx, draft)

    async def _rule_action(self, ctx: Context, action: str, rule_id: str) -> None:
        rule = self.channels.get(ctx.actor, rule_id)
        if rule is None:
            await ctx.reply(
                "هشدار پیدا نشد یا به این مقصد تعلق ندارد.",
                [("بازگشت", "ad:list:0")],
                edit=True,
            )
        elif action == "delete":
            await ctx.reply(
                f"هشدار {rule['symbol']} و سابقهٔ آن حذف شود؟",
                [
                    ("تأیید حذف", f"ad:confirmdelete:{rule_id}"),
                    ("بازگشت", f"ad:view:{rule_id}"),
                    ("لغو", "ad:menu"),
                ],
                edit=True,
            )
        elif action == "confirmdelete":
            self.channels.remove(ctx.actor, rule_id)
            await self.list(ctx)
        elif action in ("on", "off"):
            if not await self.channels.set_enabled(ctx.actor, rule_id, action == "on"):
                await ctx.reply("هشدار تغییر کرده یا حذف شده است.")
            else:
                await self.list(ctx)
        elif action == "edit":
            self.settings.pop(ctx.scope, None)
            definition = rule["definition"]
            conditions = (
                definition["conditions"]
                if definition["type"] == "GROUP"
                else [definition]
            )
            if any(c["type"] == "GROUP" for c in conditions):
                await ctx.reply(
                    "ویرایش گروه‌های تودرتو در این رابط پشتیبانی نمی‌شود. مشاهده، تغییر وضعیت و حذف همچنان ممکن است.",
                    [("بازگشت", f"ad:view:{rule_id}")],
                    edit=True,
                )
                return
            draft = ChannelDraft(
                self.now() + 600_000,
                self.channels.channel_id,
                step="builder",
                operator=definition.get("operator", "AND"),
                conditions=list(conditions),
                instrument=Instrument(rule["ins_code"], rule["symbol"], rule["symbol"]),
                rule_id=rule_id,
                revision=rule["revision"],
            )
            self.drafts[ctx.scope] = draft
            await self.show(ctx, draft)
        elif action == "view":
            await ctx.reply(
                f"نماد: {rule['symbol']}\nوضعیت: {'روشن' if rule['enabled'] else 'خاموش'}\n{tree_text(parse_tree(rule['definition']))}",
                [
                    ("ویرایش", f"ad:edit:{rule_id}"),
                    ("تنظیمات پیشرفته", f"ad:qualityc:{rule_id}"),
                    (
                        "غیرفعال کردن" if rule["enabled"] else "فعال کردن",
                        f"ad:{'off' if rule['enabled'] else 'on'}:{rule_id}",
                    ),
                    ("حذف", f"ad:delete:{rule_id}"),
                    ("بازگشت", "ad:list:0"),
                    ("لغو", "ad:menu"),
                ],
                edit=True,
            )

    async def _draft_callback(self, ctx: Context, token: str, action: str) -> None:
        draft = self.drafts.get(ctx.scope)
        if ctx.group_id and (
            draft is None or draft.token != token or ctx.message_id != draft.message_id
        ):
            return
        if draft is None or draft.token != token or not self._live(ctx.scope, draft):
            if draft and not self._live(ctx.scope, draft):
                self.drafts.pop(ctx.scope, None)
            await ctx.reply(
                "این مرحله منقضی یا تغییر کرده است. از آخرین پیام یا /admin استفاده کنید.",
                [("مدیریت", "ad:menu")],
                edit=True,
            )
            return
        if action == "cancel":
            await self.menu(ctx)
            return
        if draft.saving:
            return
        if action == "note" and draft.instrument:
            await self.compose_note(ctx, draft.instrument)
            return
        if action == "back" and draft.step == "type" and draft.family:
            draft.family = (
                "life"
                if draft.family in ("formed", "weakened", "gone", "reformed")
                else ""
            )
        elif draft.step == "type" and action in (
            "familylife",
            "familyformed",
            "familyweakened",
            "familygone",
            "familyreformed",
            "familyvolume",
            "familypeak",
            "familyrapid",
        ):
            draft.family = action[6:]
        elif action == "back":
            if draft.step == "channel":
                await self.menu(ctx)
                return
            draft.step = {
                "search": "channel",
                "results": "search",
                "type": "builder" if draft.conditions else "search",
                "percent": "type"
                if draft.selected_type.endswith(("PEAK_DROP_PERCENT", "WEAKENED"))
                else "window",
                "window": "type",
                "minimum": "percent"
                if draft.selected_type.endswith(("DROP_PERCENT", "WEAKENED"))
                else "type",
                "builder": "search",
                "hysteresis": "minimum",
                "duration": "minimum",
            }[draft.step]
        elif draft.step == "channel" and action == "channel":
            draft.step = "search"
        elif draft.step == "results" and re.fullmatch(r"symbol[0-7]", action):
            index = int(action[6:])
            if index >= len(draft.results):
                return
            item = draft.results[index]
            if (
                draft.rule_id
                and draft.instrument
                and draft.instrument.ins_code != item.ins_code
            ):
                await self.show(ctx, draft, "برای تغییر نماد، هشدار جدید بسازید.")
                return
            draft.instrument, draft.step = (
                item,
                "builder" if draft.conditions else "type",
            )
        elif (
            draft.step == "type"
            and re.fullmatch(r"type[0-9]+", action)
            and int(action[4:]) < len(ADMIN_TYPES)
        ):
            draft.selected_type = ADMIN_TYPES[int(action[4:])]
            draft.percent, draft.window, draft.minimum = None, None, None
            draft.hysteresis, draft.legacy_volume = 5, int(action[4:]) in (4, 5)
            draft.disappearance_seconds = 0
            if draft.selected_type in LIFECYCLE_LABELS:
                draft.minimum = 0
            draft.step = first_step(draft.selected_type, "percent")
        elif draft.step == "window" and action in ("window1", "window5", "window15"):
            draft.window, draft.step = int(action[6:]), "percent"
        elif draft.step == "percent" and action in (
            "percent10",
            "percent20",
            "percent30",
            "percent50",
            "percent75",
        ):
            draft.percent, draft.step = float(action[7:]), "minimum"
            draft.minimum = 0
        elif (
            draft.step == "minimum"
            and action == "hysteresis"
            and not draft.legacy_volume
        ):
            draft.step = "hysteresis"
        elif (
            draft.step == "minimum"
            and action == "duration"
            and draft.selected_type.endswith("REFORMED")
        ):
            draft.step = "duration"
        elif draft.step == "minimum" and action in (
            "volume1000000",
            "volume5000000",
            "volume10000000",
        ):
            await self.message(
                replace(
                    ctx, message_text=action[6:], reply_to_message_id=draft.message_id
                )
            )
            return
        elif draft.step in ("percent", "minimum") and action == "custom":
            pass
        elif action == "keep" and draft.step == "type" and draft.selected_type:
            draft.step = first_step(draft.selected_type, "percent")
        elif action == "keep" and draft.step == "window" and draft.window is not None:
            draft.step = "percent"
        elif action == "keep" and draft.step in ("percent", "minimum"):
            value = draft.percent if draft.step == "percent" else draft.minimum
            if value is None:
                return
            await self.message(
                replace(
                    ctx, message_text=str(value), reply_to_message_id=draft.message_id
                )
            )
            return
        elif draft.step == "builder" and action == "operator":
            draft.operator = "OR" if draft.operator == "AND" else "AND"
        elif draft.step == "builder" and action == "add" and len(draft.conditions) < 8:
            draft.edit_index, draft.step = None, "type"
            draft.selected_type = ""
            draft.family = ""
            draft.percent, draft.window, draft.minimum = None, None, None
            draft.hysteresis, draft.legacy_volume = 5, False
        elif draft.step == "builder" and re.fullmatch(r"(?:edit|remove)[0-7]", action):
            index = int(action[-1])
            if index >= len(draft.conditions):
                return
            if action.startswith("edit"):
                draft.edit_index, draft.step = index, "type"
                draft.family = ""
                condition = draft.conditions[index]
                draft.selected_type = condition["type"]
                config = condition["config"]
                draft.percent, draft.window = (
                    config.get("thresholdPercent"),
                    config.get("windowMinutes"),
                )
                draft.minimum = config.get(
                    "minimumBaselineVolume",
                    config.get(
                        "minimumPreviousVolume",
                        config.get(
                            "minimumPeakVolume",
                            config.get("thresholdVolume", config.get("minimumVolume")),
                        ),
                    ),
                )
                draft.hysteresis = config.get("hysteresisPercent", 5)
                draft.legacy_volume = "minimumVolume" in config
                draft.disappearance_seconds = config.get(
                    "minimumDisappearanceSeconds", 0
                )
            else:
                draft.conditions.pop(index)
                draft.edit_index = None
        elif (
            draft.step == "builder"
            and action == "save"
            and draft.instrument
            and draft.conditions
        ):
            await self._save(ctx, draft)
            return
        else:
            return
        await self.show(ctx, draft)

    async def _save(self, ctx: Context, draft: ChannelDraft) -> None:
        if draft.instrument is None:
            return
        definition = draft.tree()
        draft.saving = True

        def guard() -> bool:
            return self._live(ctx.scope, draft)

        try:
            if draft.rule_id:
                if not await self.channels.update(
                    ctx.actor,
                    draft.rule_id,
                    definition,
                    revision=draft.revision,
                    guard=guard,
                ):
                    self.drafts.pop(ctx.scope, None)
                    await ctx.reply(
                        "هشدار تغییر کرده یا حذف شده است؛ دوباره انتخاب کنید.",
                        [("بازگشت", "ad:list:0")],
                        edit=True,
                    )
                    return
            else:
                await self.channels.create(
                    ctx.actor,
                    dict(
                        insCode=draft.instrument.ins_code,
                        symbol=draft.instrument.symbol,
                        name=draft.instrument.name,
                    ),
                    definition,
                    guard=guard,
                )
        except Exception:
            draft.saving = False
            if self._live(ctx.scope, draft):
                await self.show(
                    ctx,
                    draft,
                    "ذخیره یا بررسی اجازهٔ ارسال مقصد ناموفق بود. دسترسی را بررسی و دوباره تلاش کنید.",
                )
            return
        self.drafts.pop(ctx.scope, None)
        await ctx.reply(
            f"هشدار برای مقصد «{self.channels.status(ctx.actor)['title']}» ذخیره شد.\n{LIVE_NOTICE}",
            [("مشاهده هشدارها", "ad:list:0"), ("بازگشت", "ad:menu")],
            edit=True,
        )

    async def message(self, ctx: Context) -> None:
        self.channels.authorize(ctx.actor)
        if ctx.scope in self.settings:
            await self._quality_message(ctx)
            return
        outgoing = self.messages.get(ctx.scope)
        if outgoing:
            if (
                self.now() > outgoing.expires
                or outgoing.channel_id != self.channels.channel_id
            ):
                self.messages.pop(ctx.scope, None)
                await ctx.reply("پیش‌نویس منقضی شد. با /admin دوباره شروع کنید.")
                return
            if ctx.group_id and ctx.reply_to_message_id != outgoing.message_id:
                return
            if outgoing.preview:
                await self._show_message(
                    ctx, "برای تغییر متن، ابتدا «ویرایش متن» را بزنید."
                )
                return
            try:
                outgoing.body = text(
                    ctx.message_text, 500 if outgoing.instrument else 1500
                )
            except ValueError:
                await self._show_message(
                    ctx, "متن خالی یا بیش از حد طولانی است؛ متن کوتاه‌تری بفرستید."
                )
                return
            outgoing.preview = True
            await self._show_message(ctx)
            return
        draft = self.drafts.get(ctx.scope)
        if draft is None:
            return
        if ctx.group_id and (
            draft.message_id is None or ctx.reply_to_message_id != draft.message_id
        ):
            return
        if not self._live(ctx.scope, draft):
            self.drafts.pop(ctx.scope, None)
            await ctx.reply("فرایند منقضی شد. با /admin دوباره شروع کنید.")
            return
        if draft.saving:
            return
        if draft.step == "search":
            query = ctx.message_text.strip()
            if not 1 <= len(query) <= 80:
                await self.show(ctx, draft, "نام نماد باید بین ۱ تا ۸۰ نویسه باشد.")
                return
            token = draft.token
            results = (await self.market.search(query))[:8]
            if not self._live(ctx.scope, draft) or token != draft.token:
                return
            self.channels.authorize(ctx.actor)
            draft.results = results
            draft.step = "results" if results else "search"
            await self.show(
                ctx, draft, "" if results else "نمادی پیدا نشد. دوباره جست‌وجو کنید."
            )
            return
        try:
            value = numeric(ctx.message_text)
            if draft.step == "percent":
                if not 0 < value <= 100 or round(value, 6) != value:
                    raise ValueError("Invalid percentage")
                draft.percent, draft.step = value, "minimum"
                if draft.minimum is None:
                    draft.minimum = 0
            elif draft.step == "hysteresis":
                if not 0 <= value <= 100 or round(value, 6) != value:
                    raise ValueError("Invalid hysteresis")
                draft.hysteresis, draft.step = value, "minimum"
            elif draft.step == "duration":
                draft.disappearance_seconds, draft.step = (
                    integer(value, 0, 86_400),
                    "minimum",
                )
            elif draft.step == "minimum":
                minimum = integer(value, 1 if "VOLUME_" in draft.selected_type else 0)
                config = wizard_config(
                    draft.selected_type,
                    draft.percent,
                    draft.window,
                    minimum,
                    draft.hysteresis,
                    draft.legacy_volume,
                    draft.disappearance_seconds,
                )
                condition = definition_json(
                    parse_tree(dict(type=draft.selected_type, config=config))
                )
                if draft.edit_index is not None:
                    draft.conditions[draft.edit_index] = condition
                elif len(draft.conditions) < 8:
                    draft.conditions.append(condition)
                draft.edit_index, draft.step = None, "builder"
            else:
                await self.show(ctx, draft, "از دکمه‌های آخرین پیام استفاده کنید.")
                return
        except ValueError, TypeError, KeyError, OverflowError:
            await self.show(ctx, draft, numeric_error(draft.step, draft.selected_type))
            return
        await self.show(ctx, draft)

    def _quality_live(self, ctx: Context, draft: QualityDraft) -> bool:
        self.channels.authorize(ctx.actor)
        return (
            self.settings.get(ctx.scope) is draft
            and self.now() <= draft.expires
            and draft.channel_id == self.channels.channel_id
        )

    async def _show_quality(self, ctx: Context, notice: str = "") -> None:
        draft = self.settings[ctx.scope]
        if not self._quality_live(ctx, draft):
            self.settings.pop(ctx.scope, None)
            await ctx.reply("پیش‌نویس منقضی شد. دوباره هشدار را انتخاب کنید.")
            return
        draft.token = secrets.token_hex(4)
        p = parse_policy(draft.settings)
        summary = (
            f"تنظیمات پیشرفتهٔ هشدار\nاهمیت پایه: {SEVERITY_LABELS[p.severity]}\n"
            + "مرزهای اهمیت: "
            + (
                "؛ ".join(
                    f"{SEVERITY_LABELS[t.severity]} از {fa(t.minimum_drop_percent)}٪"
                    for t in p.thresholds
                )
                or "بدون مرز"
            )
            + f"\nحذف تکرار: تا بازیابی کامل؛ مستقل برای هر هشدار\nتشدید: {'روشن' if p.escalation_enabled else 'خاموش'}"
            + f"\nافزایش لازم برای تشدید: {fa(p.additional_drop_percent)} واحد درصد\nفاصلهٔ تشدید: {fa(p.minimum_interval_seconds)} ثانیه"
            + f"\nفاصلهٔ اعلان‌ها: {fa(draft.cooldown)} ثانیه\nثبت نتیجه: {'روشن' if p.outcome_tracking else 'خاموش'}"
            + "\nدرصدها توصیهٔ معامله نیستند. تشدید فقط برای شرط کاهش صف با خط مبنای ثابت است."
        )
        prompts = dict(
            thresholds="دو درصد صعودی برای «مهم» و «بحرانی» با فاصله بنویسید؛ مثلاً ۵۰ ۷۵. برای حذف مرزها فقط ۰ بفرستید.",
            additional_drop_percent="حداقل افزایش کاهش صف را به واحد درصد بنویسید؛ بزرگ‌تر از صفر تا ۱۰۰.",
            minimum_interval_seconds="حداقل فاصلهٔ تشدید را به ثانیه بنویسید؛ ۱ تا ۸۶۴۰۰.",
            cooldown="فاصلهٔ اعلان‌ها را به ثانیه بنویسید؛ ۰ تا ۸۶۴۰۰.",
        )
        actions = [
            (label, "severity" + severity)
            for severity, label in SEVERITY_LABELS.items()
        ]
        actions += [
            ("مرزهای اهمیت", "thresholds"),
            ("تغییر وضعیت تشدید", "escalation"),
            ("حداقل افزایش کاهش", "additional_drop_percent"),
            ("فاصلهٔ تشدید", "minimum_interval_seconds"),
            ("فاصلهٔ اعلان‌ها", "cooldown"),
            ("تغییر وضعیت ثبت نتیجه", "outcomes"),
            ("تأیید و ذخیرهٔ تنظیمات بالا", "save"),
        ]
        if draft.field:
            summary += "\n\n" + prompts[draft.field]
            actions = [("بازگشت", "back")]
        actions.append(("لغو", "cancel"))
        await ctx.reply(
            (notice + "\n" if notice else "") + summary,
            [(label, f"qf:{draft.token}:{action}") for label, action in actions],
            edit=True,
        )

    async def _quality_callback(self, ctx: Context) -> None:
        match = re.fullmatch(r"qf:([a-f0-9]{8}):([a-z_]+)", ctx.data)
        draft = self.settings.get(ctx.scope)
        if (
            not match
            or not draft
            or not self._quality_live(ctx, draft)
            or match[1] != draft.token
        ):
            await ctx.reply("این انتخاب منقضی یا تغییر کرده است.")
            return
        action = match[2]
        if draft.saving:
            return
        if action == "cancel":
            self.settings.pop(ctx.scope, None)
            await ctx.reply("لغو شد؛ تنظیمات ذخیره نشد.")
            return
        if action == "save" and not draft.field:
            # Consume before permission awaits; duplicate clicks cannot save twice.
            draft.token = ""
            draft.saving = True

            def guard() -> bool:
                return self._quality_live(ctx, draft)

            try:
                if draft.private and self.alerts:
                    changed = self.alerts.update(
                        ctx.actor,
                        draft.rule["id"],
                        draft.rule["definition"],
                        quality=draft.settings,
                        cooldown=draft.cooldown,
                        revision=draft.rule["revision"],
                    )
                else:
                    changed = await self.channels.update(
                        ctx.actor,
                        draft.rule["id"],
                        draft.rule["definition"],
                        revision=draft.rule["revision"],
                        quality=draft.settings,
                        cooldown=draft.cooldown,
                        guard=guard,
                    )
            except Exception:
                draft.saving = False
                if self._quality_live(ctx, draft):
                    await self._show_quality(
                        ctx, "ذخیره یا بررسی دسترسی ناموفق بود؛ دوباره تلاش کنید."
                    )
                return
            self.settings.pop(ctx.scope, None)
            await ctx.reply(
                "تنظیمات ذخیره شد."
                if changed
                else "هشدار تغییر کرده یا حذف شده است؛ دوباره انتخاب کنید."
            )
            return
        if draft.field and action != "back":
            return
        if action.startswith("severity") and action[8:] in SEVERITIES:
            draft.settings["severity"] = action[8:]
        elif action == "escalation":
            draft.settings["escalation_enabled"] = not draft.settings[
                "escalation_enabled"
            ]
        elif action == "outcomes":
            draft.settings["outcome_tracking"] = not draft.settings["outcome_tracking"]
        elif action in (
            "thresholds",
            "additional_drop_percent",
            "minimum_interval_seconds",
            "cooldown",
        ):
            draft.field = action
        elif action == "back":
            draft.field = ""
        await self._show_quality(ctx)

    async def _quality_message(self, ctx: Context) -> None:
        draft = self.settings[ctx.scope]
        if draft.saving:
            return
        if not self._quality_live(ctx, draft):
            self.settings.pop(ctx.scope, None)
            await ctx.reply("پیش‌نویس منقضی شد.")
            return
        if not draft.field:
            await self._show_quality(ctx, "برای تغییر مقدار، دکمهٔ آن را بزنید.")
            return
        settings = dict(draft.settings)
        try:
            if draft.field == "thresholds":
                values = [numeric(v) for v in ctx.message_text.split()]
                if values == [0]:
                    settings["thresholds"] = []
                elif len(values) == 2:
                    settings["thresholds"] = [
                        dict(severity=s, minimum_drop_percent=v)
                        for s, v in zip(("important", "critical"), values, strict=True)
                    ]
                else:
                    raise ValueError("Two thresholds required")
            elif draft.field == "cooldown":
                cooldown = integer(numeric(ctx.message_text), 0, 86_400)
                draft.cooldown = cooldown
            else:
                settings[draft.field] = numeric(ctx.message_text)
            draft.settings = dict(parse_policy(settings).to_json())
            draft.field = ""
        except ValueError, TypeError, OverflowError:
            await self._show_quality(
                ctx, "مقدار نامعتبر است؛ طبق راهنما دوباره بنویسید."
            )
            return
        await self._show_quality(ctx)
