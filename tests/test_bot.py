"""Bot commands/callbacks use MockTransport and disposable SQLite only."""

import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fake_telegram import FakeTelegramAPI as TelegramAPI
from reference import reference
from test_market_client import ITEM, NOW, synthetic_book
from test_services import DROP, GROUP

from trade_alert.alerts import AlertService
from trade_alert.bot import TelegramBot
from trade_alert.bot_text import history_text
from trade_alert.channels import ChannelService
from trade_alert.database import open_database, rows
from trade_alert.market_service import MarketService
from trade_alert.telegram import TelegramError
from trade_alert.users import UserService

TOKEN = "123456:" + "x" * 35
CHANNEL = "-1001234567890"


def message(actor=1, content="/start", *, chat=None, kind="private", update_id=1):
    return dict(
        update_id=update_id,
        message=dict(
            message_id=1,
            date=1,
            **{"from": dict(id=actor, is_bot=False, first_name="User")},
            chat=dict(id=actor if chat is None else chat, type=kind),
            text=content,
        ),
    )


def callback(actor, data, *, chat=None, kind="private", update_id=1):
    value = message(actor, chat=chat, kind=kind, update_id=update_id)["message"]
    value["from"] = dict(id=123456, is_bot=True, first_name="Bot")
    return dict(
        update_id=update_id,
        callback_query=dict(
            id=f"callback{update_id}",
            **{"from": dict(id=actor, is_bot=False)},
            message=value,
            data=data,
        ),
    )


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_status_distinguishes_destination_permission_and_network_failures(
        self,
    ):
        await self.send("/admin")
        for error, category, explanation in (
            (
                TelegramError(400, "DESTINATION_INVALID"),
                "DESTINATION_INVALID",
                "اطلاعات این مقصد را نپذیرفت",
            ),
            (
                TelegramError(403, "DESTINATION_INACCESSIBLE"),
                "DESTINATION_INACCESSIBLE",
                "دسترسی بات به این مقصد را رد",
            ),
            (
                TelegramError(403, "ADMIN_REQUIRED"),
                "ADMIN_REQUIRED",
                "دسترسی ادمین می‌خواهد",
            ),
            (TelegramError(0, "NETWORK"), "NETWORK", "خطای اتصال"),
            (TelegramError(429, retry_after=10), "RATE_LIMITED", "موقتاً محدود"),
            (
                RuntimeError("PRIVATE_DIAGNOSTIC_MUST_NOT_LEAK"),
                "UNAVAILABLE",
                "علت مشخصی ثبت نشده",
            ),
        ):
            self.channels.verify = AsyncMock(side_effect=error)
            await self.click("ad:status")
            self.assertIn(explanation, self.texts()[-1])
            self.assertNotIn("PRIVATE_DIAGNOSTIC_MUST_NOT_LEAK", self.texts()[-1])
            self.assertFalse(self.channels.status("1")["can_post"])
            self.assertEqual(self.channels.status("1")["permission_error"], category)

    async def test_admin_message_requires_preview_confirmation_and_rechecks_rights(
        self,
    ):
        await self.send("/admin")
        await self.press("ارسال پیام به گروه یا کانال")
        await self.send("متن پیام آزمایشی <بدون قالب‌بندی>")
        confirm = self.button("تأیید ارسال به مقصد")

        def sent():
            return [
                p
                for m, p in self.calls
                if m == "sendMessage" and str(p["chat_id"]) == CHANNEL
            ]

        self.assertFalse(sent())
        await self.click(confirm, actor=3)
        self.assertFalse(sent())
        await self.click(confirm)
        await self.click(confirm)
        self.assertEqual(
            [p["text"] for p in sent()], ["متن پیام آزمایشی <بدون قالب‌بندی>"]
        )
        self.assertNotIn("1", self.bot.admin.messages)
        for reason in ("permission", "revoked", "expired", "cancelled", "reconfigured"):
            await self.send("/admin")
            await self.press("ارسال پیام به گروه یا کانال")
            await self.send("این پیام نباید ارسال شود")
            confirm = self.button("تأیید ارسال به مقصد")
            old_admins, old_channel = self.admins[:], self.channels.channel_id
            if reason == "permission":
                self.can_post = False
            elif reason == "revoked":
                self.admins.remove("1")
            elif reason == "expired":
                self.clock += 600001
            elif reason == "cancelled":
                await self.send("/cancel")
            else:
                self.channels.channel_id = "-100999999"
            await self.click(confirm)
            self.assertEqual(len(sent()), 1)
            self.admins[:] = old_admins
            self.channels.channel_id = old_channel
            self.can_post = True
        await self.send("/admin")
        await self.press("ارسال پیام به گروه یا کانال")
        await self.send("عدم ارسال پس از لغو دسترسی در بررسی مجوز")

        async def revoke_during_verification(destination):
            self.admins.remove("1")
            return dict(channelId=destination, title="Group", canPost=True)

        self.channels.verify = revoke_during_verification
        await self.press("تأیید ارسال به مقصد")
        self.assertEqual(len(sent()), 1)

    async def test_manual_send_failure_is_not_retried_by_duplicate_confirmation(self):
        await self.send("/admin")
        await self.press("ارسال پیام به گروه یا کانال")
        await self.send("پیام آزمایشی")
        confirm = self.button("تأیید ارسال به مقصد")

        async def fail_send(method, payload):
            if method == "sendMessage" and str(payload["chat_id"]) == CHANNEL:
                return httpx.Response(503, json=dict(ok=False, error_code=503))

        self.hook = fail_send
        await self.click(confirm)
        self.assertIn("ممکن است پیام رسیده باشد", self.texts()[-1])
        await self.click(confirm)
        outbound = [
            p
            for m, p in self.calls
            if m == "sendMessage" and str(p["chat_id"]) == CHANNEL
        ]
        self.assertEqual(len(outbound), 1)

    async def test_symbol_note_optional_shared_edit_delete_and_rule_draft_resume(self):
        await self.watch()
        await self.press("افزودن یا ویرایش یادداشت گروه")
        await self.send("یادداشت مشترک نماد")
        confirm = self.button("تأیید و ذخیره")
        self.assertEqual(self.channels.symbol_note(ITEM.ins_code), "")
        await self.click(confirm, actor=3)
        self.assertEqual(self.channels.symbol_note(ITEM.ins_code), "")
        await self.click(confirm)
        self.assertEqual(self.channels.symbol_note(ITEM.ins_code), "یادداشت مشترک نماد")
        await self.send("/admin", actor=2)
        await self.press("ساخت هشدار جدید", actor=2)
        await self.press("انتخاب این مقصد مشترک", actor=2)
        await self.send("خودرو", actor=2)
        await self.press(ITEM.symbol, actor=2)
        self.assertIn("یادداشت مشترک نماد", self.texts()[-1])
        await self.press("ویرایش یادداشت نماد", actor=2)
        await self.send("x" * 501, actor=2)
        self.assertIn("طولانی", self.texts()[-1])
        await self.send("یادداشت ویرایش‌شده", actor=2)
        await self.press("تأیید و ذخیره", actor=2)
        self.assertEqual(self.bot.admin.drafts["2"].step, "type")
        self.assertEqual(self.channels.symbol_note(ITEM.ins_code), "یادداشت ویرایش‌شده")
        await self.press("ویرایش یادداشت نماد", actor=2)
        await self.press("حذف یادداشت", actor=2)
        await self.press("تأیید و ذخیره", actor=2)
        self.assertEqual(self.channels.symbol_note(ITEM.ins_code), "")

    async def test_group_note_requires_reply_and_cannot_post_manual_messages(self):
        await self.watch()
        self.enable_group_replies()
        await self.group_send("/alert")
        await self.group_press("انتخاب این گروه")
        await self.group_send("خودرو")
        await self.group_press(ITEM.symbol)
        await self.group_press("ویرایش یادداشت نماد")
        await self.group_send("نادیده", reply=False)
        self.assertFalse(self.bot.admin.messages[f"{CHANNEL}:1"].preview)
        await self.group_send("یادداشت گروه")
        confirm = self.button("تأیید و ذخیره")
        await self.group_click(confirm, actor=2)
        self.assertEqual(self.channels.symbol_note(ITEM.ins_code), "")
        await self.group_click(confirm)
        self.assertEqual(self.channels.symbol_note(ITEM.ins_code), "یادداشت گروه")
        await self.group_click("ad:post")
        self.assertNotIn(f"{CHANNEL}:1", self.bot.admin.messages)

    async def test_help_examples_are_separate_and_work_in_the_private_wizard(self):
        await self.send("/help")
        help_text = self.texts()[-1]
        self.assertIn("«خودرو» در مثال‌ها نام یک نماد بورسی است", help_text)
        for command in (
            "/add خودرو",
            "/queue خودرو",
            "/remove خودرو",
            "/remove rule RULE_ID",
        ):
            self.assertIn(command, help_text.splitlines())
        self.assertLess(len(help_text), 1750)  # No split through a command/example.
        await self.watch()
        await self.press("ساخت هشدار برای این نماد")
        self.assertIn("وضعیت «بدون صف»", self.texts()[-1])
        await self.press("کاهش صف خرید")
        self.assertIn("فاصلهٔ ارسال پیام‌ها نیست", self.texts()[-1])
        await self.press("۵ دقیقه")
        self.assertIn("۱۰۰٬۰۰۰ به ۷۰٬۰۰۰", self.texts()[-1])
        await self.send("۳۰")
        self.assertIn("۱۰۰۰۰۰", self.texts()[-1])
        await self.send("۱۰۰۰۰۰")
        await self.press("تأیید و ذخیره")
        rule = self.alerts.list("1")[0]
        self.assertEqual(rule["definition"]["config"]["thresholdPercent"], 30)
        self.assertEqual(rule["definition"]["config"]["minimumBaselineVolume"], 100000)

    async def group_send(self, content, actor=1, *, reply=True, chat=CHANNEL):
        self.sequence += 1
        update = message(
            actor, content, chat=int(chat), kind="supergroup", update_id=self.sequence
        )
        draft = self.bot.admin.drafts.get(f"{chat}:{actor}")
        if reply and draft and draft.message_id:
            update["message"]["reply_to_message"] = dict(message_id=draft.message_id)
        await self.bot.handle_update(update)

    async def group_click(self, data, actor=1, *, chat=CHANNEL):
        self.sequence += 1
        update = callback(
            actor, data, chat=int(chat), kind="supergroup", update_id=self.sequence
        )
        update["callback_query"]["message"]["message_id"] = self.group_message_id
        await self.bot.handle_update(update)

    async def group_press(self, label, actor=1):
        await self.group_click(self.button(label), actor)

    def enable_group_replies(self):
        self.chat_type = "supergroup"
        self.group_message_id = 100

        async def hook(method, payload):
            if method in ("sendMessage", "editMessageText"):
                if method == "sendMessage":
                    self.group_message_id += 1
                else:
                    self.group_message_id = payload["message_id"]
                return httpx.Response(
                    200,
                    json=dict(ok=True, result=dict(message_id=self.group_message_id)),
                )

        self.hook = hook

    async def test_group_commands_build_manage_and_delete_only_group_rules(self):
        await self.watch()
        private = self.alerts.create("1", ITEM.ins_code, DROP)
        self.enable_group_replies()
        await self.group_send("/admin@test_bot")
        await self.group_press("ساخت هشدار جدید")
        await self.group_press("انتخاب این گروه")
        self.search.reset_mock()
        await self.group_send("unrelated admin conversation", reply=False)
        self.search.assert_not_awaited()
        await self.group_send("خودرو")
        await self.group_press(ITEM.symbol)
        await self.group_press("از بین رفتن صف خرید")
        await self.group_send("۰")
        saved = self.button("تأیید و ذخیره")
        await self.group_click(saved)
        await self.group_click(saved)
        group_rules = self.channels.list("1")
        self.assertEqual(len(group_rules), 1)
        self.assertEqual(group_rules[0]["channel_id"], CHANNEL)
        self.assertEqual(len(self.alerts.list("1")), 1)
        await self.group_send("/alerts")
        self.assertNotIn(private, str(self.calls[-1]))
        await self.group_click("ad:view:" + group_rules[0]["id"])
        await self.group_press("حذف")
        await self.group_press("تأیید حذف")
        self.assertFalse(self.channels.list("1"))
        self.assertIsNotNone(self.alerts.get("1", private))

    async def test_group_never_registers_users_or_accepts_other_groups_nonadmins(self):
        self.enable_group_replies()
        await self.group_send("/admin")
        self.assertFalse(self.calls)
        self.assertEqual(rows(self.db, "SELECT * FROM users"), [])
        await self.send("/start")
        await self.send("/start", 3)
        self.calls.clear()
        for actor, chat in ((1, "-999"), (2, CHANNEL), (3, CHANNEL), (9, CHANNEL)):
            await self.group_send("/admin", actor, chat=chat)
            await self.group_click("ad:new", actor, chat=chat)
        self.assertFalse(self.calls)
        self.assertEqual(len(rows(self.db, "SELECT * FROM users")), 2)
        await self.group_send("/admin@another_bot")
        self.assertFalse(self.calls)
        await self.group_send("/admin")
        self.assertEqual(str(self.calls[-1][1]["chat_id"]), CHANNEL)
        self.admins.remove("1")
        self.calls.clear()
        await self.group_click("ad:new")
        self.assertFalse(self.calls)

    async def test_group_replies_stop_after_bot_demotion_or_admin_revocation_during_check(
        self,
    ):
        await self.send("/start")
        self.enable_group_replies()
        self.member_status = "member"
        self.calls.clear()
        await self.group_send("/admin")
        self.assertFalse(any(method == "sendMessage" for method, _ in self.calls))
        self.member_status = "administrator"
        original = self.hook

        async def revoke(method, payload):
            if method == "getChatMember":
                self.admins.remove("1")
            return await original(method, payload)

        self.hook = revoke
        self.calls.clear()
        await self.group_send("/admin")
        self.assertFalse(any(method == "sendMessage" for method, _ in self.calls))

    async def test_group_private_buttons_preferences_and_drafts_are_separate(self):
        await self.watch()
        await self.send("/admin")
        await self.press("ساخت هشدار جدید")
        private_draft = self.bot.admin.drafts["1"]
        private_token = private_draft.token
        self.enable_group_replies()
        await self.group_send("/alert")
        group_draft = self.bot.admin.drafts[f"{CHANNEL}:1"]
        self.assertIsNot(group_draft, private_draft)
        await self.group_click(f"af:{private_token}:channel")
        self.assertEqual(private_draft.step, "channel")
        self.assertEqual(group_draft.step, "channel")
        await self.send("/start", 2)
        before = sum(
            method in ("sendMessage", "editMessageText") for method, _ in self.calls
        )
        await self.group_click(f"af:{group_draft.token}:channel", actor=2)
        self.assertEqual(
            sum(
                method in ("sendMessage", "editMessageText") for method, _ in self.calls
            ),
            before,
        )
        self.assertEqual(group_draft.step, "channel")
        await self.group_click("notifications:off")
        self.assertTrue(self.users.preferences("1")["notifications_enabled"])
        for command in ("/list", "/history", "/queue خودرو", "/remove خودرو"):
            await self.group_send(command)
            self.assertIn("خصوصی", self.texts()[-1])
            self.assertNotIn(ITEM.symbol, self.texts()[-1])
        await self.group_send("/cancel")
        self.assertIn("1", self.bot.admin.drafts)
        self.assertNotIn(f"{CHANNEL}:1", self.bot.admin.drafts)
        await self.group_click("ad:status")
        self.assertIn("خصوصی", self.texts()[-1])
        self.assertNotIn("آخرین خطای", self.texts()[-1])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        context = open_database(Path(directory.name) / "bot.db")
        self.db = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.calls = []
        self.clock = NOW
        self.sequence = 1
        self.hook = None
        self.can_post = True
        self.chat_type = "channel"
        self.member_status = "administrator"
        self.member_id = 123456

        async def transport(request):
            method = request.url.path.rsplit("/", 1)[1]
            payload = json.loads(request.content)
            self.calls.append((method, payload))
            if self.hook:
                response = await self.hook(method, payload)
                if response is not None:
                    return response
            result = (
                dict(id=123456, is_bot=True, username="test_bot")
                if method == "getMe"
                else dict(id=int(CHANNEL), type=self.chat_type, title="کانال آزمایشی")
                if method == "getChat"
                else dict(
                    status=self.member_status,
                    can_post_messages=self.can_post,
                    user=dict(id=self.member_id, is_bot=True),
                )
                if method == "getChatMember"
                else []
                if method == "getUpdates"
                else True
            )
            return httpx.Response(200, json=dict(ok=True, result=result))

        self.api = TelegramAPI(TOKEN, transport=httpx.MockTransport(transport))
        self.api.bot_id, self.api.username = 123456, "test_bot"
        self.addAsyncCleanup(self.api.close)
        self.users = UserService(self.db, ["1", "2", "3"], lambda: self.clock)
        self.alerts = AlertService(self.db, self.users, now=lambda: self.clock)
        self.search = AsyncMock(return_value=[ITEM])
        self.fetch = AsyncMock(
            side_effect=lambda item: {
                **synthetic_book(item, self.clock),
                "verified": False,
            }
        )
        self.market = MarketService(self.fetch, self.search)
        self.addAsyncCleanup(self.market.close)
        self.admins = ["1", "2"]
        self.channels = ChannelService(
            self.db,
            self.users,
            self.admins,
            CHANNEL,
            self.api.verify_channel,
            now=lambda: self.clock,
        )
        self.status = dict(
            is_running=True,
            is_market_open=True,
            force_active=False,
            schedule_configured=True,
            monitored_symbols=2,
            fresh_symbols=0,
            last_verified_snapshot=None,
            max_age_ms=30_000,
            poll_interval_ms=1000,
            last_polling_cycle=NOW,
            last_successful_fetch=NOW,
            consecutive_errors=1,
            rate_limited_until=0,
            symbols=[dict(symbol="PRIVATE_OTHER_SYMBOL")],
        )
        self.logger = logging.getLogger("offline-bot-test")
        self.logger.disabled = True
        self.bot = TelegramBot(
            self.api,
            self.users,
            self.market,
            self.alerts,
            lambda: self.status,
            self.channels,
            now=lambda: self.clock,
            logger=self.logger,
        )
        self.addAsyncCleanup(self.bot.stop)

    async def send(self, content, actor=1):
        self.sequence += 1
        await self.bot.handle_update(message(actor, content, update_id=self.sequence))

    async def click(self, data, actor=1):
        self.sequence += 1
        await self.bot.handle_update(callback(actor, data, update_id=self.sequence))

    def button(self, label):
        for _, payload in reversed(self.calls):
            keyboard = payload.get("reply_markup", {}).get("inline_keyboard")
            if keyboard is not None:
                for row in keyboard:
                    for button in row:
                        if button["text"] == label:
                            return button["callback_data"]
                self.fail(f"Missing latest button: {label}")
        self.fail(f"No keyboard: {label}")

    async def press(self, label, actor=1):
        await self.click(self.button(label), actor)

    def texts(self):
        return [
            p["text"] for m, p in self.calls if m in ("sendMessage", "editMessageText")
        ]

    async def watch(self, actor=1):
        await self.send("/add خودرو", actor)
        await self.click(f"add:{ITEM.ins_code}", actor)

    async def new_channel_draft(self):
        await self.send("/admin")
        await self.press("ساخت هشدار جدید")
        await self.press("انتخاب این مقصد مشترک")
        await self.send("خودرو")
        await self.press(ITEM.symbol)
        await self.press("از بین رفتن صف خرید")
        await self.send("۰")
        return self.button("تأیید و ذخیره")

    async def private_draft(self):
        await self.watch()
        await self.send("/alert")
        await self.press(ITEM.symbol)
        await self.press("کاهش صف خرید")
        await self.press("۵ دقیقه")
        await self.send("۳۰")
        await self.send("۱٬۰۰۰٬۰۰۰")
        return self.button("تأیید و ذخیره")

    async def test_authorization_precedes_registration_callbacks_and_all_api_calls(
        self,
    ):
        updates = [
            message(9),
            callback(9, "help"),
            message(1, kind="group", chat=-100),
            callback(1, "help", kind="supergroup", chat=-100),
            callback(1, "help", chat=2),
            message(1, chat=2),
            dict(update_id=1),
            dict(update_id=1, channel_post=message()["message"]),
            dict(
                update_id=1,
                callback_query=dict(
                    id="inline",
                    **{"from": dict(id=1, is_bot=False)},
                    inline_message_id="forged",
                    data="help",
                ),
            ),
        ]
        for invalid_id in (True, "1", 0, -1, 2**53):
            invalid = message()
            invalid["message"]["from"]["id"] = invalid_id
            updates.append(invalid)
        for command in (
            "/add خودرو",
            "/list",
            "/queue",
            "/alert",
            "/alerts",
            "/history",
            "/remove خودرو",
            "/status",
            "/admin",
        ):
            updates.append(message(9, command))
        for update in updates:
            await self.bot.handle_update(update)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM users").fetchone()[0], 0)
        self.search.assert_not_called()
        self.fetch.assert_not_called()

    async def test_onboarding_buttons_search_and_symbol_shortcut(self):
        await self.send("/start")
        self.assertIn("هشدار زنده صادر نمی‌شود", self.texts()[-1])
        await self.press("افزودن نماد")
        await self.send("خودرو")
        self.search.assert_awaited_once_with("خودرو")
        await self.click(f"add:{ITEM.ins_code}")
        await self.press("ساخت هشدار برای این نماد")
        self.assertEqual(self.bot.flows["1"].ins_code, ITEM.ins_code)
        self.assertEqual(self.bot.flows["1"].step, "type")
        self.assertIn("هشدار زنده صادر نمی‌شود", self.texts()[-1])
        await self.click(f"newalert:{ITEM.ins_code}", 2)
        self.assertNotIn("2", self.bot.flows)

    async def test_read_commands_preserve_drafts_and_resume_refreshes_token(self):
        save = await self.private_draft()
        flow = self.bot.flows["1"]
        for command in ("/help", "/status", "/start", "/list", "/alerts", "/history"):
            await self.send(command)
            self.assertIs(self.bot.flows["1"], flow)
        await self.click("nav:resume")
        self.assertNotEqual(save, self.button("تأیید و ذخیره"))
        self.assertEqual(flow.step, "confirm")
        await self.send("/cancel")
        self.assertNotIn("1", self.bot.flows)
        await self.new_channel_draft()
        draft = self.bot.admin.drafts["1"]
        for command in ("/help", "/status", "/start"):
            await self.send(command)
            self.assertIs(self.bot.admin.drafts["1"], draft)
        await self.press("ادامهٔ پیش‌نویس")
        self.assertEqual(draft.step, "builder")
        self.clock += 600_001
        await self.click("nav:resume")
        self.assertNotIn("1", self.bot.admin.drafts)
        self.assertIn("منقضی", self.texts()[-1])

    async def test_group_help_and_status_preserve_reply_scoped_draft(self):
        await self.private_draft()
        private = self.bot.flows["1"]
        self.search.reset_mock()
        self.enable_group_replies()
        await self.group_send("/alert")
        self.assertIs(self.bot.flows["1"], private)
        self.assertNotIn("کنار گذاشته", self.texts()[-1])
        await self.group_press("انتخاب این گروه")
        scope = f"{CHANNEL}:1"
        draft = self.bot.admin.drafts[scope]
        for command in ("/help", "/status"):
            await self.group_send(command)
            self.assertIs(self.bot.admin.drafts[scope], draft)
        await self.group_send("خودرو", reply=False)
        self.search.assert_not_awaited()
        await self.group_send("خودرو")
        self.search.assert_awaited_once()
        await self.group_click("nav:alert")
        self.assertIs(self.bot.flows["1"], private)

    async def test_switching_private_and_shared_editors_discards_previous_draft(self):
        await self.private_draft()
        private = self.alerts.create("1", ITEM.ins_code, DROP)
        await self.click("ad:new")
        self.assertNotIn("1", self.bot.flows)
        self.assertIn("1", self.bot.admin.drafts)
        self.assertTrue(
            any("پیش‌نویس خصوصی قبلی کنار گذاشته" in t for t in self.texts())
        )
        await self.click(f"ar:edit:{private}")
        self.assertNotIn("1", self.bot.admin.drafts)
        self.assertEqual(self.bot.flows["1"].rule_id, private)

    async def test_deletion_confirmation_preserves_other_owners_and_channel_rules(self):
        await self.watch()
        await self.watch(2)
        private = self.alerts.create("1", ITEM.ins_code, DROP)
        other = self.alerts.create("2", ITEM.ins_code, DROP)
        shared = await self.channels.create(
            "1", dict(insCode=ITEM.ins_code, symbol=ITEM.symbol, name=ITEM.name), GROUP
        )
        await self.send(f"/remove rule {private}")
        self.assertIsNotNone(self.alerts.get("1", private))
        await self.press("بازگشت")
        await self.send("/remove خودرو")
        self.assertIsNotNone(self.alerts.get("1", private))
        await self.press("انصراف")
        self.assertTrue(self.users.list_symbols("1"))
        await self.send("/remove خودرو")
        confirmation = self.button("حذف نماد و هشدارها")
        await self.click(confirmation, 3)
        self.assertIsNotNone(self.alerts.get("1", private))
        await self.click(confirmation)
        self.assertIsNone(self.alerts.get("1", private))
        self.assertIsNotNone(self.alerts.get("2", other))
        self.assertIsNotNone(self.channels.get("1", shared))
        self.assertTrue(self.users.list_symbols("2"))

    async def test_edit_keeps_existing_values_and_reports_new_status(self):
        await self.watch()
        rule = self.alerts.create("1", ITEM.ins_code, DROP)
        await self.click(f"ar:edit:{rule}")
        await self.press("نگه‌داشتن نوع فعلی")
        for _ in range(3):
            self.assertIn("مقدار فعلی", self.texts()[-1])
            await self.press("نگه‌داشتن مقدار فعلی")
        await self.press("تأیید و ذخیره")
        self.assertEqual(self.alerts.get("1", rule)["definition"], DROP)
        await self.click(f"ar:off:{rule}")
        self.assertIn("خاموش شد", self.texts()[-1])
        await self.press("مشاهدهٔ هشدار")
        await self.press("بازگشت به هشدارها")
        await self.new_channel_draft()
        await self.press("افزودن شرط")
        await self.press("کاهش صف فروش")
        await self.press("۵ دقیقه")
        await self.send("۳۰")
        await self.send("۱۰۰")
        draft = self.bot.admin.drafts["1"]
        expected = list(draft.conditions)
        await self.press("ویرایش شرط ۲")
        await self.press("نگه‌داشتن نوع فعلی")
        for _ in range(3):
            await self.press("نگه‌داشتن مقدار فعلی")
        self.assertEqual(draft.conditions, expected)
        await self.press("ویرایش شرط ۱")
        await self.press("نگه‌داشتن نوع فعلی")
        await self.press("نگه‌داشتن مقدار فعلی")
        self.assertEqual(draft.conditions, expected)
        await self.press("تأیید و ذخیره")
        self.assertIn("کانال آزمایشی", self.texts()[-1])
        self.assertIn("هشدار زنده صادر نمی‌شود", self.texts()[-1])

    async def test_input_errors_recovery_and_preferences_are_explicit(self):
        await self.watch()
        await self.send("/alert")
        self.assertNotIn(
            "بازگشت",
            [
                b["text"]
                for row in self.calls[-1][1]["reply_markup"]["inline_keyboard"]
                for b in row
            ],
        )
        await self.press(ITEM.symbol)
        await self.press("کاهش صف خرید")
        await self.press("۱ دقیقه")
        await self.send("۱۰۱")
        self.assertIn("درصد نامعتبر", self.texts()[-1])
        await self.send("۳۰")
        self.assertIn("ابتدای بازه", self.texts()[-1])
        await self.send("۱٫۵")
        self.assertIn("عدد صحیح", self.texts()[-1])
        await self.click("preferences")
        self.assertIn("روی هشدارهای گروه یا کانال اثر ندارد", self.texts()[-1])
        await self.send("/unknown")
        self.assertIn("دستور شناخته نشد", self.texts()[-1])
        self.assertIn("1", self.bot.flows)

    async def test_new_search_and_private_resume_expiry_and_cancellation(self):
        await self.send("/add")
        self.clock += 300_001
        await self.send("خودرو")
        self.search.assert_not_awaited()
        self.assertIn("منقضی", self.texts()[-1])
        await self.send("/add")
        await self.send("/cancel")
        await self.send("خودرو")
        self.search.assert_not_awaited()
        await self.private_draft()
        self.clock += 600_001
        await self.click("nav:resume")
        self.assertNotIn("1", self.bot.flows)
        self.assertIn("منقضی", self.texts()[-1])

    async def test_search_watchlists_preferences_and_queue_are_owner_bound(self):
        await self.send("/add خودرو")
        await self.click(f"add:{ITEM.ins_code}", 2)
        self.assertEqual(self.users.list_symbols("2"), [])
        await self.click(f"add:{ITEM.ins_code}")
        await self.click(f"queue:{ITEM.ins_code}", 2)
        await self.click(f"remove:{ITEM.ins_code}", 2)
        self.fetch.assert_not_called()
        await self.click("notifications:off", 2)
        await self.send("/start", 2)
        self.assertEqual(self.users.preferences("2")["notifications_enabled"], 0)
        self.assertEqual(self.users.preferences("1")["notifications_enabled"], 1)
        await self.send("/queue خودرو")
        self.assertIn("قابل تأیید نیست", self.texts()[-1])
        self.fetch.assert_awaited_once()
        self.assertTrue(
            all(
                p["chat_id"] in ("1", "2")
                for m, p in self.calls
                if m in ("sendMessage", "editMessageText")
            )
        )
        await self.send("/remove خودرو")
        self.assertTrue(self.users.list_symbols("1"))
        await self.press("حذف نماد و هشدارها")
        self.assertEqual(self.users.list_symbols("1"), [])

    async def test_search_expiry_and_stale_async_results(self):
        await self.send("/add خودرو")
        self.clock += 300_001
        await self.click(f"add:{ITEM.ins_code}")
        self.assertEqual(self.users.list_symbols("1"), [])
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(query):
            entered.set()
            await release.wait()
            return [ITEM]

        self.search.side_effect = slow
        pending = asyncio.create_task(self.send("/add قدیمی"))
        await entered.wait()
        self.search.side_effect = None
        self.search.return_value = []
        await self.send("/add جدید")
        release.set()
        await pending
        self.assertEqual(self.bot.searches["1"].instruments, [])

    async def test_private_creation_refreshes_tokens_and_confirms_once(self):
        await self.watch()
        await self.send("/alert")
        stale = self.button(ITEM.symbol)
        await self.press(ITEM.symbol)
        await self.click(stale)
        self.assertIn("تغییر", self.texts()[-1])
        await self.press("کاهش صف خرید")
        await self.press("۵ دقیقه")
        for invalid in ("۰", "۱۰۱", "nan", "۱٫۰۰۰۰۰۰۱"):
            await self.send(invalid)
            self.assertIn("نامعتبر", self.texts()[-1])
        await self.send("۳۰")
        await self.send("-1")
        self.assertIn("نامعتبر", self.texts()[-1])
        await self.send("۱٬۰۰۰٬۰۰۰")
        save = self.button("تأیید و ذخیره")
        await asyncio.gather(self.click(save), self.click(save))
        rules = self.alerts.list("1")
        self.assertEqual(len(rules), 1)
        self.assertEqual(
            rules[0]["definition"],
            dict(
                type="BUY_QUEUE_DROP_PERCENT",
                config=dict(
                    thresholdPercent=30,
                    windowMinutes=5,
                    minimumBaselineVolume=1_000_000,
                ),
            ),
        )
        self.fetch.assert_not_called()

    async def test_private_rule_actions_history_and_stale_edits_are_scoped(self):
        await self.watch()
        rule = self.alerts.create("1", ITEM.ins_code, DROP)
        for action in ("view", "on", "off", "edit", "delete", "confirmdelete"):
            await self.click(f"ar:{action}:{rule}", 2)
        self.assertEqual(self.alerts.get("1", rule)["revision"], 1)
        await self.send(f"/remove rule {rule}", 2)
        self.assertIsNotNone(self.alerts.get("1", rule))
        await self.click(f"ar:edit:{rule}")
        await self.press("از بین رفتن صف فروش")
        await self.send("۰")
        save = self.button("تأیید و ذخیره")
        self.alerts.set_enabled("1", rule, False)
        await self.click(save)
        self.assertIn("تغییر کرده", self.texts()[-1])
        for i in range(12):
            self.db.execute(
                "INSERT INTO alert_events (id, rule_id, message, created_at) VALUES (?, ?, ?, ?)",
                (f"event{i}", rule, f"mine-{i}", i),
            )
        self.calls.clear()
        await self.send("/history")
        self.assertEqual(len(self.texts()), 10)
        self.assertEqual(self.texts()[0], "mine-11")
        await self.click(f"ar:delete:{rule}")
        self.assertIsNotNone(self.alerts.get("1", rule))
        await self.press("تأیید حذف")
        self.assertIsNone(self.alerts.get("1", rule))

    async def test_public_status_never_renders_other_users_or_channel_metadata(self):
        await self.send("/status", 3)
        rendered = self.texts()[-1]
        self.assertNotIn("PRIVATE_OTHER_SYMBOL", rendered)
        self.assertNotIn(CHANNEL, rendered)
        self.assertIn("کافی نیست", rendered)
        self.assertIn("موفق API به‌تنهایی", rendered)
        await self.send("/admin", 3)
        for action in ("ad:status", "ad:new", "ad:list:0", "af:00000000:save"):
            self.calls.clear()
            await self.click(action, 3)
            self.assertEqual([m for m, _ in self.calls], ["answerCallbackQuery"])

    async def test_channel_flat_builder_edits_removes_validates_and_saves_once(self):
        await self.new_channel_draft()
        await self.press("افزودن شرط")
        await self.press("حداقل حجم صف خرید")
        await self.send("۰")
        self.assertIn("نامعتبر", self.texts()[-1])
        await self.send("۵۰۰")
        await self.press("ویرایش شرط ۲")
        await self.press("از بین رفتن صف فروش")
        await self.send("۱۰۰")
        await self.press("حذف شرط ۲")
        await self.press("افزودن شرط")
        await self.press("حداقل حجم صف خرید")
        await self.send("۵۰۰")
        await self.press("تغییر به: حداقل یکی از شرط‌ها")
        save = self.button("تأیید و ذخیره")
        await asyncio.gather(self.click(save), self.click(save))
        rules = self.channels.list("1")
        self.assertEqual(len(rules), 1)
        self.assertEqual(
            rules[0]["definition"],
            dict(
                type="GROUP",
                operator="OR",
                conditions=[
                    dict(
                        type="BUY_QUEUE_DISAPPEARED",
                        config=dict(minimumPreviousVolume=0),
                    ),
                    dict(type="BUY_QUEUE_VOLUME_ABOVE", config=dict(minimumVolume=500)),
                ],
            ),
        )
        self.assertTrue(any(m == "editMessageText" for m, _ in self.calls))
        self.assertFalse(
            any(
                p.get("chat_id") == CHANNEL for m, p in self.calls if m == "sendMessage"
            )
        )
        self.fetch.assert_not_called()

    async def test_channel_callbacks_do_not_accept_private_rule_or_reveal_creator(self):
        await self.watch()
        await self.send("/admin", 2)
        private = self.alerts.create("1", ITEM.ins_code, DROP)
        for action in ("view", "edit", "on", "off", "delete", "confirmdelete"):
            await self.click(f"ad:{action}:{private}", 2)
        self.assertEqual(self.alerts.get("1", private)["revision"], 1)
        channel = await self.channels.create(
            "1", dict(insCode=ITEM.ins_code, symbol=ITEM.symbol, name=ITEM.name), GROUP
        )
        self.calls.clear()
        await self.click(f"ad:view:{channel}", 2)
        rendered = json.dumps(self.calls, ensure_ascii=False)
        self.assertNotIn("owner", rendered)
        self.assertNotIn("سازنده", rendered)
        await self.click(f"ar:confirmdelete:{channel}")
        self.assertIsNotNone(self.channels.get("1", channel))

    async def test_channel_back_cancel_expiry_and_nested_ui_limits(self):
        await self.send("/admin")
        await self.press("ساخت هشدار جدید")
        stale = self.button("انتخاب این مقصد مشترک")
        await self.press("انتخاب این مقصد مشترک")
        await self.press("بازگشت")
        fresh = self.button("انتخاب این مقصد مشترک")
        await self.click(stale)
        self.assertIn("تغییر", self.texts()[-1])
        await self.click(fresh)
        self.clock += 600_001
        await self.send("خودرو")
        self.assertIn("منقضی", self.texts()[-1])
        await self.new_channel_draft()
        save = self.button("تأیید و ذخیره")
        await self.press("لغو")
        await self.click(save)
        self.assertEqual(self.channels.list("1"), [])
        nested = await self.channels.create(
            "1",
            dict(insCode=ITEM.ins_code, symbol=ITEM.symbol, name=ITEM.name),
            {**GROUP, "conditions": [GROUP]},
        )
        await self.click(f"ad:edit:{nested}")
        self.assertIn("تودرتو", self.texts()[-1])

    async def test_permission_denial_and_revision_race_never_save(self):
        save = await self.new_channel_draft()
        self.can_post = False
        await self.click(save)
        self.assertEqual(self.channels.list("1"), [])
        self.assertIn("اجازه", self.texts()[-1])
        self.can_post = True
        await self.press("تأیید و ذخیره")
        rule = self.channels.list("1")[0]
        await self.click(f"ad:edit:{rule['id']}")
        save = self.button("تأیید و ذخیره")
        await self.send("/admin", 2)
        await self.channels.set_enabled("2", rule["id"], False)
        await self.click(save)
        self.assertIn("تغییر کرده", self.texts()[-1])
        self.assertFalse(self.channels.get("1", rule["id"])["enabled"])

    async def test_expiry_cancel_reconfiguration_and_revocation_during_permission_await(
        self,
    ):
        for change in ("expire", "cancel", "reconfigure", "revoke"):
            with self.subTest(change=change):
                self.clock = NOW
                self.admins[:] = ["1", "2"]
                self.channels.channel_id = CHANNEL
                save = await self.new_channel_draft()
                entered, release = asyncio.Event(), asyncio.Event()

                async def delayed(method, payload):
                    if method == "getChatMember":
                        entered.set()
                        await release.wait()

                self.hook = delayed
                pending = asyncio.create_task(self.click(save))
                await entered.wait()
                if change == "expire":
                    self.clock += 600_001
                elif change == "cancel":
                    await self.send("/cancel")
                elif change == "reconfigure":
                    self.channels.channel_id = "-100999"
                else:
                    self.admins.remove("1")
                release.set()
                await pending
                self.hook = None
                self.assertEqual(
                    self.db.execute("SELECT count(*) FROM alert_rules").fetchone()[0], 0
                )

    async def test_startup_checks_exact_channel_bot_membership_and_post_rights(self):
        self.api.bot_id = None
        await self.bot.initialize()
        self.assertEqual(
            [m for m, _ in self.calls][:3], ["getMe", "getChat", "getChatMember"]
        )
        self.assertEqual(self.db.execute("SELECT count(*) FROM users").fetchone()[0], 0)
        for attribute, invalid in (
            ("chat_type", "private"),
            ("can_post", False),
            ("member_status", "member"),
            ("member_id", 999),
        ):
            old = getattr(self, attribute)
            setattr(self, attribute, invalid)
            self.assertFalse((await self.api.verify_channel(CHANNEL))["canPost"])
            setattr(self, attribute, old)
        self.assertFalse(any(m == "sendMessage" for m, _ in self.calls))

    async def test_edit_not_modified_does_not_send_duplicate_and_other_edit_errors_fallback(
        self,
    ):
        await self.send("/admin")
        for description in (
            "Bad Request: message is not modified",
            "Bad Request: message to edit not found",
        ):

            async def error(method, payload):
                if method == "editMessageText":
                    return httpx.Response(
                        400,
                        json=dict(ok=False, error_code=400, description=description),
                    )

            self.hook = error
            self.calls.clear()
            await self.click("ad:menu")
            self.assertEqual(
                any(m == "sendMessage" for m, _ in self.calls),
                "not found" in description,
            )

    async def test_polling_offsets_success_and_shutdown_join(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        polls = 0

        async def poll(method, payload):
            nonlocal polls
            if method != "getUpdates":
                return
            polls += 1
            if polls == 1:
                return httpx.Response(
                    200,
                    json=dict(
                        ok=True,
                        result=[
                            message(9, update_id=5),
                            callback(1, "notifications:off", update_id=4),
                            message(1, update_id=3),
                        ],
                    ),
                )
            self.assertEqual(payload["offset"], 6)
            self.assertEqual(payload["timeout"], 30)
            self.assertEqual(payload["allowed_updates"], ["message", "callback_query"])
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.hook = poll
        task = asyncio.create_task(self.bot.run())
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(self.bot.last_telegram_success, NOW)
        self.assertEqual(self.users.preferences("1")["notifications_enabled"], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM users").fetchone()[0], 1)
        await asyncio.wait_for(self.bot.stop(), 1)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(task.done())
        self.assertFalse(self.api.active)

    async def test_polling_rate_limit_delay_is_cancellable_and_conflict_stops(self):
        entered = asyncio.Event()

        async def rate_limit(method, payload):
            if method == "getUpdates":
                return httpx.Response(
                    429,
                    json=dict(
                        ok=False,
                        error_code=429,
                        parameters=dict(retry_after=60),
                        description=TOKEN,
                    ),
                )

        async def wait(seconds):
            self.assertEqual(seconds, 60)
            entered.set()
            await asyncio.Event().wait()

        self.hook = rate_limit
        with patch("trade_alert.bot.asyncio.sleep", side_effect=wait):
            task = asyncio.create_task(self.bot.run())
            await asyncio.wait_for(entered.wait(), 2)
            await self.bot.stop()
        self.assertTrue(task.done())
        self.assertIsNone(self.bot.last_telegram_success)

        async def conflict(method, payload):
            if method == "getUpdates":
                return httpx.Response(
                    409, json=dict(ok=False, error_code=409, description=TOKEN)
                )

        self.hook = conflict
        with self.assertRaises(TelegramError) as error:
            await self.bot.run()
        self.assertEqual(error.exception.code, 409)
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertFalse(self.bot.flows)

    async def test_transport_errors_are_sanitized_and_close_cancels_native_request(
        self,
    ):
        entered = asyncio.Event()

        async def hanging(method, payload):
            entered.set()
            await asyncio.Event().wait()

        self.hook = hanging
        with self.assertRaises(TelegramError) as error:
            await self.api.call("getMe", timeout=0.01)
        self.assertEqual(error.exception.kind, "NETWORK")
        entered.clear()
        task = asyncio.create_task(self.api.call("getMe"))
        await entered.wait()
        await self.api.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(logging.getLogger("httpx").disabled)

    async def test_draft_restart_discards_pending_private_and_channel_choices(self):
        save = await self.private_draft()
        self.bot.flows.clear()  # A fresh process has no in-memory drafts.
        await self.click(save)
        self.assertEqual(self.alerts.list("1"), [])
        save = await self.new_channel_draft()
        await self.bot.stop()
        await self.click(save)
        self.assertEqual(self.channels.list("1"), [])

    async def test_ui_workflow_business_state_matches_typescript(self):
        steps = [
            dict(text="/start"),
            dict(text="/add خودرو"),
            dict(data=f"add:{ITEM.ins_code}"),
            dict(actor=2, data=f"add:{ITEM.ins_code}"),
            dict(actor=2, data="notifications:off"),
            dict(text="/alert"),
            dict(press=ITEM.symbol),
            dict(press="کاهش صف خرید"),
            dict(press="۵ دقیقه"),
            dict(text="۳۰"),
            dict(text="۱٬۰۰۰٬۰۰۰"),
            dict(press="تأیید و ذخیره"),
            dict(text="/admin"),
            dict(press="ساخت هشدار جدید"),
            dict(press="انتخاب این کانال"),
            dict(text="خودرو"),
            dict(press=ITEM.symbol),
            dict(press="ناپدید شدن صف خرید"),
            dict(text="۰"),
            dict(press="افزودن شرط"),
            dict(press="حداقل حجم صف خرید"),
            dict(text="۵۰۰"),
            dict(press="تغییر به: حداقل یکی از شرط‌ها"),
            dict(press="تأیید و ذخیره"),
            dict(text="/remove خودرو"),
            dict(actor=3, text="/admin"),
        ]
        expected = reference(
            "bot-reference",
            dict(
                now=NOW,
                channel=CHANNEL,
                instrument=dict(
                    insCode=ITEM.ins_code, symbol=ITEM.symbol, name=ITEM.name
                ),
                steps=steps,
            ),
        )
        actual = []
        for step in steps:
            actor = step.get("actor", 1)
            if "press" in step:
                # Keep the captured Node inputs/expectations unchanged; adapt UI labels only.
                label = (
                    step["press"]
                    .replace("ناپدید شدن صف", "از بین رفتن صف")
                    .replace("انتخاب این کانال", "انتخاب این مقصد مشترک")
                )
                await self.press(label, actor)
            elif "data" in step:
                await self.click(step["data"], actor)
            else:
                await self.send(step["text"], actor)
                if step["text"] == "/remove خودرو":
                    await self.press("حذف نماد و هشدارها", actor)
            rules = rows(
                self.db,
                "SELECT owner_id, channel_id, ins_code, definition, revision, enabled, cooldown_seconds FROM alert_rules ORDER BY channel_id, owner_id",
            )
            for rule in rules:
                rule["definition"] = json.loads(rule["definition"])
            actual.append(
                dict(
                    users=rows(
                        self.db,
                        "SELECT telegram_user_id, private_chat_id, notifications_enabled FROM users ORDER BY telegram_user_id",
                    ),
                    watchlists=rows(
                        self.db,
                        "SELECT user_id, ins_code FROM watchlists ORDER BY user_id, ins_code",
                    ),
                    rules=rules,
                    searches=self.search.call_count,
                )
            )
        self.assertEqual(actual, expected)

    async def test_eight_leaf_limit_and_channel_drop_inputs(self):
        await self.new_channel_draft()
        for _ in range(7):
            await self.press("افزودن شرط")
            await self.press("حداقل حجم صف فروش")
            await self.send("۱")
        keyboard = self.calls[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertNotIn("افزودن شرط", [b["text"] for row in keyboard for b in row])
        draft = self.bot.admin.drafts["1"]
        await self.click(f"af:{draft.token}:add")
        self.assertEqual(len(draft.conditions), 8)
        await self.press("ویرایش شرط ۸")
        await self.press("کاهش صف فروش")
        await self.press("۱ دقیقه")
        for invalid in ("۰", "۱۰۱", "۳۰٫۱۲۳۴۵۶۷"):
            await self.send(invalid)
            self.assertIn("نامعتبر", self.texts()[-1])
        await self.send("۲۵٫۱۲۳۴۵۶")
        await self.send("۰")
        await self.press("تأیید و ذخیره")
        leaves = self.channels.list("1")[0]["definition"]["conditions"]
        self.assertEqual(len(leaves), 8)
        self.assertEqual(
            leaves[-1],
            dict(
                type="SELL_QUEUE_DROP_PERCENT",
                config=dict(
                    thresholdPercent=25.123456, windowMinutes=1, minimumBaselineVolume=0
                ),
            ),
        )

    async def test_channel_pagination_adoption_status_and_confirmed_delete(self):
        await self.send("/admin")
        ids = [
            await self.channels.create(
                "1",
                dict(insCode=ITEM.ins_code, symbol=ITEM.symbol, name=ITEM.name),
                GROUP,
            )
            for _ in range(11)
        ]
        await self.send("/admin", 2)
        await self.click("ad:list:0", 2)
        self.assertEqual(self.button("بعدی"), "ad:list:1")
        await self.press("بعدی", 2)
        self.assertEqual(self.button("قبلی"), "ad:list:0")
        await self.click(f"ad:off:{ids[0]}", 2)
        self.assertFalse(self.channels.get("2", ids[0])["enabled"])
        await self.click(f"ad:on:{ids[0]}", 2)
        self.assertEqual(self.channels.get("2", ids[0])["owner_id"], "2")
        self.channels.record_error("PERMISSION", NOW)
        await self.click("ad:status", 2)
        self.assertIn("PERMISSION", self.texts()[-1])
        await self.click(f"ad:delete:{ids[0]}", 2)
        self.assertEqual(len(self.channels.list("2")), 11)
        await self.press("تأیید حذف", 2)
        self.assertEqual(len(self.channels.list("2")), 10)
        self.assertEqual(self.channels.status("2")["last_error"]["code"], "PERMISSION")

    async def test_duplicate_channel_save_waiting_for_permissions_inserts_once(self):
        save = await self.new_channel_draft()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(method, payload):
            if method == "getChatMember":
                entered.set()
                await release.wait()

        self.hook = delayed
        first = asyncio.create_task(self.click(save))
        await entered.wait()
        await self.click(save)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM alert_rules").fetchone()[0], 0
        )
        release.set()
        await first
        self.assertEqual(len(self.channels.list("1")), 1)

    async def test_new_and_composite_history_preserves_values_in_private_scope(self):
        await self.watch()
        rule = self.alerts.create("1", ITEM.ins_code, DROP)
        payload = dict(
            type="BUY_QUEUE_DROP_PERCENT",
            symbol=ITEM.symbol,
            timestamp=NOW,
            previousVolume=1000,
            currentVolume=500,
            actualPercent=50,
            windowMinutes=1,
        )
        self.db.execute(
            "INSERT INTO alert_events (id, rule_id, payload, message, created_at) VALUES ('new', ?, ?, '', ?)",
            (rule, json.dumps(payload), NOW),
        )
        await self.send("/history")
        self.assertIn("۱٬۰۰۰", self.texts()[-1])
        self.assertIn("۵۰۰", self.texts()[-1])
        self.assertIn("میلادی", self.texts()[-1])
        self.calls.clear()
        await self.send("/history", 2)
        self.assertNotIn(ITEM.symbol, "\n".join(self.texts()))
        composite = dict(
            type="COMPOSITE",
            symbol=ITEM.symbol,
            timestamp=NOW,
            operator="AND",
            matches=[
                dict(
                    path="0.0",
                    condition=dict(
                        type="BUY_QUEUE_VOLUME_ABOVE", config=dict(minimumVolume=500)
                    ),
                    previousVolume=600,
                    currentVolume=600,
                    actualPercent=0,
                    windowMinutes=None,
                )
            ],
        )
        self.assertIn(
            "۶۰۰", history_text(dict(payload=json.dumps(composite), message=""))
        )

    async def test_cancelled_edit_during_permission_check_does_not_advance_revision(
        self,
    ):
        await self.click(await self.new_channel_draft())
        rule = self.channels.list("1")[0]
        await self.click(f"ad:edit:{rule['id']}")
        save = self.button("تأیید و ذخیره")
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(method, payload):
            if method == "getChatMember":
                entered.set()
                await release.wait()

        self.hook = delayed
        pending = asyncio.create_task(self.click(save))
        await entered.wait()
        await self.send("/cancel")
        release.set()
        await pending
        current = self.channels.get("1", rule["id"])
        self.assertEqual(
            (current["revision"], current["definition"]), (1, rule["definition"])
        )

    async def test_wrong_channel_identity_and_http_redirects_fail_closed(self):
        async def wrong(method, payload):
            if method == "getChat":
                return httpx.Response(
                    200,
                    json=dict(
                        ok=True, result=dict(id=-100999, type="channel", title="Wrong")
                    ),
                )

        self.hook = wrong
        self.assertFalse((await self.api.verify_channel(CHANNEL))["canPost"])
        self.assertFalse(any(m == "getChatMember" for m, _ in self.calls))

        async def redirect(method, payload):
            return httpx.Response(302, headers={"Location": "https://never.invalid/"})

        self.hook = redirect
        self.calls.clear()
        with self.assertRaises(TelegramError) as error:
            await self.api.call("getMe")
        self.assertEqual(error.exception.code, 302)
        self.assertEqual(len(self.calls), 1)

    async def test_errors_never_log_raw_payloads_or_bot_token(self):
        self.search.side_effect = RuntimeError(TOKEN)
        with patch.object(self.logger, "error") as log:
            await self.send("/add خودرو")
        self.assertEqual(log.call_args.args, ("Telegram command failed",))
        self.assertNotIn(TOKEN, str(log.call_args))

        async def network_error(method, payload):
            raise httpx.ReadError(TOKEN)

        self.hook = network_error
        with self.assertRaises(TelegramError) as error:
            await self.api.call("getMe")
        self.assertNotIn(TOKEN, str(error.exception))
