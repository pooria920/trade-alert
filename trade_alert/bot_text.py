"""Persian plain-text presentation. Dates explicitly use the Gregorian calendar."""

import json
from datetime import datetime, timezone
from typing import Any

from .composite import parse_evidence
from .definitions import Group, RuleTree, VolumeConfig
from .evaluators import DisappearedConfig, DropConfig
from .market import TEHRAN
from .validation import integer, loads, number, obj, text

LABELS = {
    "BUY_QUEUE_DROP_PERCENT": "کاهش صف خرید",
    "SELL_QUEUE_DROP_PERCENT": "کاهش صف فروش",
    "BUY_QUEUE_DISAPPEARED": "از بین رفتن صف خرید",
    "SELL_QUEUE_DISAPPEARED": "از بین رفتن صف فروش",
    "BUY_QUEUE_VOLUME_ABOVE": "حداقل حجم صف خرید",
    "SELL_QUEUE_VOLUME_ABOVE": "حداقل حجم صف فروش",
}
PRIVATE_TYPES = tuple(LABELS)[:4]
LIVE_NOTICE = "در نسخهٔ فعلی، دادهٔ صف بازار قابل‌تأیید نیست و هشدار زنده صادر نمی‌شود. تنظیمات هشدارها ذخیره می‌شوند."


def minimum_prompt(kind: str) -> str:
    label = (
        "حجم صف در ابتدای بازهٔ مقایسه"
        if kind.endswith("DROP_PERCENT")
        else "حجم صف پیش از از بین رفتن"
        if kind.endswith("DISAPPEARED")
        else "حجم فعلی صف"
    )
    return f"حداقل {label} چند سهم باشد؟ عدد صحیح بنویسید؛ مثلاً ۱۰۰٬۰۰۰." + (
        " مقدار باید بزرگ‌تر از صفر باشد."
        if kind.endswith("VOLUME_ABOVE")
        else " برای نداشتن حداقل، ۰ بفرستید."
    )


def numeric_error(step: str, kind: str) -> str:
    return (
        "درصد نامعتبر است؛ عددی بزرگ‌تر از ۰ و حداکثر ۱۰۰ با حداکثر شش رقم اعشار بنویسید؛ مثلاً ۳۰."
        if step in ("percent", "threshold")
        else "حجم نامعتبر است؛ عدد صحیح "
        + ("بزرگ‌تر از صفر" if kind.endswith("VOLUME_ABOVE") else "صفر یا بیشتر")
        + " بر حسب سهم بنویسید؛ مثلاً ۱۰۰٬۰۰۰."
    )


DIGITS = str.maketrans("0123456789,.", "۰۱۲۳۴۵۶۷۸۹٬٫")
ASCII = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫", "01234567890123456789.")


def fa(value: int | float) -> str:
    result = (
        f"{value:,.6f}".rstrip("0").rstrip(".")
        if isinstance(value, float)
        else f"{value:,}"
    )
    return result.translate(DIGITS)


def numeric(value: str) -> float:
    raw = value.strip().translate(ASCII).replace(",", "").replace("٬", "")
    if not raw:
        raise ValueError("A number is required")
    return number(float(raw), -float("inf"))


def tehran_time(timestamp: int | None) -> str:
    if timestamp is None:
        return "ثبت نشده"
    return (
        datetime.fromtimestamp(integer(timestamp) / 1000, timezone.utc)
        .astimezone(TEHRAN)
        .strftime("%Y/%m/%d %H:%M:%S")
        .translate(DIGITS)
        + " میلادی"
    )


def tree_text(tree: RuleTree) -> str:
    if isinstance(tree, Group):
        title = (
            "همهٔ شرط‌ها برقرار باشند"
            if tree.operator == "AND"
            else "حداقل یکی از شرط‌ها برقرار باشد"
        )
        return (
            title
            + ":\n"
            + "\n".join(
                f"{fa(i + 1)}. {tree_text(c)}" for i, c in enumerate(tree.conditions)
            )
        )
    label = LABELS[tree.type]
    c = tree.config
    if isinstance(c, DropConfig):
        return f"{label}: حداقل {fa(c.threshold_percent)}٪ در {fa(c.window_minutes)} دقیقه؛ حجم صف در ابتدای بازه حداقل {fa(c.minimum_baseline_volume)} سهم"
    if isinstance(c, DisappearedConfig):
        return f"{label}؛ حجم قبلی حداقل {fa(c.minimum_previous_volume)} سهم"
    return f"{label}: {fa(c.minimum_volume)} سهم"


def monitoring_text(status: dict[str, Any], now: int) -> str:
    source = status["last_verified_snapshot"]
    fresh = source is not None and 0 <= now - source <= status["max_age_ms"]
    service = (
        "متوقف"
        if not status["is_running"]
        else "در انتظار نشست"
        if not status["is_market_open"]
        else "بدون نماد"
        if not status["monitored_symbols"]
        else "فعال؛ دادهٔ معتبر و تازه کافی نیست"
        if not fresh
        or status["fresh_symbols"] != status["monitored_symbols"]
        or status["consecutive_errors"]
        else "فعال؛ دادهٔ معتبر و تازه دریافت شده"
    )
    market = (
        "اجباری برای آزمایش"
        if status["force_active"]
        else "نشست پیکربندی نشده"
        if not status["schedule_configured"]
        else "باز طبق برنامهٔ تنظیم‌شده"
        if status["is_market_open"]
        else "بسته طبق برنامهٔ تنظیم‌شده"
    )
    return (
        f"وضعیت پایش بورس\nوضعیت سرویس: {service}\nوضعیت بازار: {market}\n{LIVE_NOTICE}\n\nآمار کلی پایش\nتعداد نمادهای تحت نظر: {fa(status['monitored_symbols'])}\nنماد با دادهٔ معتبر و تازه: {fa(status['fresh_symbols'])}\n\nجزئیات دریافت داده\nفاصلهٔ بررسی: {fa(status['poll_interval_ms'] / 1000)} ثانیه\nآخرین چرخه: {tehran_time(status['last_polling_cycle'])}\nآخرین دریافت موفق از منبع بازار: {tehran_time(status['last_successful_fetch'])}\nآخرین زمان دادهٔ معتبر: {tehran_time(source)}\nخطاهای متوالی: {fa(status['consecutive_errors'])}"
        + (
            "\nدریافت موقتاً به‌علت محدودیت نرخ متوقف است."
            if status["rate_limited_until"] > now
            else ""
        )
        + "\nزمان‌ها به وقت تهران است. دریافت موفق API به‌تنهایی به معنی سلامت دادهٔ بازار نیست."
    )


def history_text(event: dict[str, Any]) -> str:
    try:
        payload = obj(loads(event["payload"]))
        symbol = text(payload["symbol"], 80)
        timestamp = integer(payload["timestamp"])
        if payload["type"] == "COMPOSITE":
            matches = parse_evidence(json.dumps(payload["matches"]))
            if not matches or payload["operator"] not in (None, "AND", "OR"):
                raise ValueError("Invalid composite event")
            lines = [
                f"{tree_text(m.condition)}\nحجم قبلی: {fa(m.previous_volume)}؛ حجم فعلی: {fa(m.current_volume)} سهم\nمیزان کاهش: {fa(m.actual_percent)}٪"
                if not isinstance(m.condition.config, VolumeConfig)
                else f"{tree_text(m.condition)}\nحجم فعلی صف: {fa(m.current_volume)} سهم"
                for m in matches
            ]
            return (
                f"هشدار ترکیبی بورس\nنماد: {symbol}\n"
                + "\n\n".join(lines)
                + f"\nزمان داده: {tehran_time(timestamp)} به وقت تهران"
            )
        kind = payload["type"]
        if kind not in PRIVATE_TYPES:
            raise ValueError("Invalid event type")
        previous, current = (
            integer(payload["previousVolume"]),
            integer(payload["currentVolume"]),
        )
        percent = number(payload["actualPercent"], 0, 100)
        window = payload["windowMinutes"]
        if window is not None:
            number(window, -float("inf"))
        return f"هشدار {LABELS[kind]}\nنماد: {symbol}\nحجم قبلی صف: {fa(previous)} سهم\nحجم فعلی صف: {fa(current)} سهم\nمیزان کاهش: {fa(percent)}٪\nبازهٔ مقایسه: {'آخرین وضعیت تأییدشده' if window is None else fa(window) + ' دقیقه'}\nزمان داده: {tehran_time(timestamp)} به وقت تهران"
    except ValueError, TypeError, KeyError, OverflowError, OSError:
        return str(event.get("message") or "هشدار قدیمی")
