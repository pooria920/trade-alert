"""Private current-admin channel UI, using expiring flat-tree drafts only."""

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .bot_text import LABELS, fa, numeric, tehran_time, tree_text
from .channels import ChannelService
from .definitions import definition_json, parse_tree
from .market import Instrument
from .market_service import MarketService
from .telegram import Buttons, Context
from .validation import integer


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
    edit_index: int | None = None
    rule_id: str | None = None
    revision: int | None = None
    saving: bool = False

    def tree(self) -> dict[str, Any]:
        return definition_json(
            parse_tree(
                dict(type="GROUP", operator=self.operator, conditions=self.conditions)
            )
        )


class AdminCommands:
    def __init__(
        self, channels: ChannelService, market: MarketService, now: Callable[[], int]
    ) -> None:
        self.channels, self.market, self.now = channels, market, now
        # ponytail: one bounded ten-minute draft per admin, discarded on restart.
        self.drafts: dict[str, ChannelDraft] = {}

    def _live(self, actor: str, draft: ChannelDraft) -> bool:
        return (
            self.drafts.get(actor) is draft
            and self.now() <= draft.expires
            and draft.channel_id == self.channels.channel_id
        )

    async def menu(self, ctx: Context) -> None:
        self.channels.authorize(ctx.actor)
        self.drafts.pop(ctx.actor, None)
        await ctx.reply(
            "مدیریت هشدارهای کانال",
            [
                ("ساخت هشدار جدید", "ad:new"),
                ("مشاهده هشدارها", "ad:list:0"),
                ("وضعیت کانال", "ad:status"),
            ],
            edit=True,
        )

    async def show(self, ctx: Context, draft: ChannelDraft, notice: str = "") -> None:
        self.channels.authorize(ctx.actor)
        draft.token = secrets.token_hex(4)
        actions: Buttons = []
        prompt = ""
        if draft.step == "channel":
            prompt = (
                f"هشدار جدید کانال\nکانال: {self.channels.status(ctx.actor)['title']}"
            )
            actions = [("انتخاب این کانال", "channel")]
        elif draft.step == "search":
            prompt = "نام نماد را برای جست‌وجو بنویسید؛ حداکثر ۸۰ نویسه."
        elif draft.step == "results":
            prompt = "نماد را انتخاب کنید:"
            actions = [
                (item.symbol, f"symbol{i}") for i, item in enumerate(draft.results)
            ]
        elif draft.step == "type":
            prompt = "شرط را انتخاب کنید:"
            actions = [(LABELS[t], f"type{i}") for i, t in enumerate(LABELS)]
        elif draft.step == "percent":
            prompt = "درصد کاهش را بنویسید؛ بزرگ‌تر از صفر تا ۱۰۰، حداکثر شش رقم اعشار."
        elif draft.step == "window":
            prompt, actions = (
                "بازهٔ مقایسه را انتخاب کنید:",
                [("۱ دقیقه", "window1"), ("۵ دقیقه", "window5")],
            )
        elif draft.step == "minimum":
            prompt = (
                "حداقل حجم فعلی صف را بنویسید؛ عدد صحیح و بزرگ‌تر از صفر."
                if draft.selected_type.endswith("VOLUME_ABOVE")
                else "حداقل حجم مبنا یا حجم قبلی صف را بنویسید؛ برای نداشتن حداقل، صفر بفرستید."
            )
        elif draft.step == "builder":
            detail = (
                tree_text(parse_tree(draft.tree()))
                if draft.conditions
                else "هنوز شرطی ثبت نشده است."
            )
            prompt = f"هشدار کانال\nکانال: {self.channels.status(ctx.actor)['title']}\nنماد: {draft.instrument.symbol if draft.instrument else ''}\n{detail}\nانتشار فقط با دادهٔ تازه و قابل تأیید انجام می‌شود."
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
        actions.extend([("بازگشت", "back"), ("لغو", "cancel")])
        await ctx.reply(
            (notice + "\n\n" if notice else "") + prompt,
            [(label, f"af:{draft.token}:{action}") for label, action in actions],
            edit=True,
        )

    async def list(self, ctx: Context, page: int = 0) -> None:
        rules = self.channels.list(ctx.actor)
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
            "هشدارهای کانال؛ برای مدیریت انتخاب کنید:"
            if rules
            else "هشداری برای این کانال ثبت نشده است.",
            buttons,
            edit=True,
        )

    async def status(self, ctx: Context) -> None:
        try:
            await self.channels.refresh()
        except Exception:
            pass
        value = self.channels.status(ctx.actor)
        error = value["last_error"]
        await ctx.reply(
            f"وضعیت کانال\nعنوان: {value['title']}\nشناسهٔ کانال: {value['channel_id']}\nاجازهٔ ارسال: {'دارد' if value['can_post'] else 'ندارد یا قابل تأیید نیست'}\nتعداد هشدارها: {fa(value['total'])}\nفعال: {fa(value['active'])}\nآخرین ارسال موفق: {tehran_time(value['last_success'])}\nآخرین خطای ارسال: {error['code'] + ' · ' + tehran_time(error['timestamp']) if error else 'ثبت نشده'}",
            [("بررسی دوباره", "ad:status"), ("بازگشت", "ad:menu")],
            edit=True,
        )

    async def callback(self, ctx: Context) -> None:
        if not self.channels.is_admin(ctx.actor):
            await ctx.answer("دسترسی ندارید.")
            return
        self.channels.authorize(ctx.actor)
        await ctx.answer()
        self.channels.authorize(ctx.actor)
        if ctx.data.startswith("af:"):
            match = re.fullmatch(r"af:([a-f0-9]{8}):(.+)", ctx.data)
            if match:
                await self._draft_callback(ctx, match[1], match[2])
            return
        action = ctx.data[3:]
        if action == "menu":
            await self.menu(ctx)
        elif action == "status":
            await self.status(ctx)
        elif action == "new":
            draft = ChannelDraft(self.now() + 600_000, self.channels.channel_id)
            self.drafts[ctx.actor] = draft
            await self.show(ctx, draft)
        elif re.fullmatch(r"list:[0-9]+", action):
            await self.list(ctx, int(action[5:]))
        elif match := re.fullmatch(
            r"(view|edit|on|off|delete|confirmdelete):([a-f0-9-]{36})", action
        ):
            await self._rule_action(ctx, match[1], match[2])

    async def _rule_action(self, ctx: Context, action: str, rule_id: str) -> None:
        rule = self.channels.get(ctx.actor, rule_id)
        if rule is None:
            await ctx.reply(
                "هشدار پیدا نشد یا به این کانال تعلق ندارد.",
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
            self.drafts[ctx.actor] = draft
            await self.show(ctx, draft)
        elif action == "view":
            await ctx.reply(
                f"نماد: {rule['symbol']}\nوضعیت: {'روشن' if rule['enabled'] else 'خاموش'}\n{tree_text(parse_tree(rule['definition']))}",
                [
                    ("ویرایش", f"ad:edit:{rule_id}"),
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
        draft = self.drafts.get(ctx.actor)
        if draft is None or draft.token != token or not self._live(ctx.actor, draft):
            if draft and not self._live(ctx.actor, draft):
                self.drafts.pop(ctx.actor, None)
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
        if action == "back":
            if draft.step == "channel":
                await self.menu(ctx)
                return
            draft.step = {
                "search": "channel",
                "results": "search",
                "type": "builder" if draft.conditions else "search",
                "percent": "type",
                "window": "percent",
                "minimum": "window"
                if draft.selected_type.endswith("DROP_PERCENT")
                else "type",
                "builder": "search",
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
        elif draft.step == "type" and re.fullmatch(r"type[0-5]", action):
            draft.selected_type = tuple(LABELS)[int(action[4:])]
            draft.percent, draft.window = None, None
            draft.step = (
                "percent" if draft.selected_type.endswith("DROP_PERCENT") else "minimum"
            )
        elif draft.step == "window" and action in ("window1", "window5"):
            draft.window, draft.step = int(action[-1]), "minimum"
        elif draft.step == "builder" and action == "operator":
            draft.operator = "OR" if draft.operator == "AND" else "AND"
        elif draft.step == "builder" and action == "add" and len(draft.conditions) < 8:
            draft.edit_index, draft.step = None, "type"
        elif draft.step == "builder" and re.fullmatch(r"(?:edit|remove)[0-7]", action):
            index = int(action[-1])
            if index >= len(draft.conditions):
                return
            if action.startswith("edit"):
                draft.edit_index, draft.step = index, "type"
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
            return self._live(ctx.actor, draft)

        try:
            if draft.rule_id:
                if not await self.channels.update(
                    ctx.actor,
                    draft.rule_id,
                    definition,
                    revision=draft.revision,
                    guard=guard,
                ):
                    self.drafts.pop(ctx.actor, None)
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
            if self._live(ctx.actor, draft):
                await self.show(
                    ctx,
                    draft,
                    "ذخیره یا بررسی اجازهٔ ارسال کانال ناموفق بود. دسترسی را بررسی و دوباره تلاش کنید.",
                )
            return
        self.drafts.pop(ctx.actor, None)
        await ctx.reply(
            "هشدار کانال ذخیره شد. انتشار فقط با دادهٔ تازه و قابل تأیید انجام می‌شود.",
            [("مشاهده هشدارها", "ad:list:0"), ("بازگشت", "ad:menu")],
            edit=True,
        )

    async def message(self, ctx: Context) -> None:
        self.channels.authorize(ctx.actor)
        draft = self.drafts.get(ctx.actor)
        if draft is None:
            return
        if not self._live(ctx.actor, draft):
            self.drafts.pop(ctx.actor, None)
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
            if not self._live(ctx.actor, draft) or token != draft.token:
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
                draft.percent, draft.step = value, "window"
            elif draft.step == "minimum":
                minimum = integer(
                    value, 1 if draft.selected_type.endswith("VOLUME_ABOVE") else 0
                )
                config = (
                    dict(
                        thresholdPercent=draft.percent,
                        windowMinutes=draft.window,
                        minimumBaselineVolume=minimum,
                    )
                    if draft.selected_type.endswith("DROP_PERCENT")
                    else dict(minimumVolume=minimum)
                    if draft.selected_type.endswith("VOLUME_ABOVE")
                    else dict(minimumPreviousVolume=minimum)
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
            await self.show(
                ctx, draft, "عدد نامعتبر است؛ مقدار را در محدودهٔ خواسته‌شده بنویسید."
            )
            return
        await self.show(ctx, draft)
