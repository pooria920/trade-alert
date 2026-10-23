"""Persian plain-text presentation. Dates explicitly use the Gregorian calendar."""

import json
from datetime import datetime, timezone
from typing import Any

from .composite import Evidence, parse_evidence
from .definitions import Group, RuleTree, VolumeConfig, parse_tree
from .evaluators import DisappearedConfig, DropConfig, PeakConfig, ThresholdConfig
from .lifecycle import LifecycleConfig
from .market import TEHRAN
from .quality import SEVERITY_LABELS
from .validation import code, integer, loads, number, obj, text

LABELS = {
    "BUY_QUEUE_DROP_PERCENT": "کاهش صف خرید",
    "SELL_QUEUE_DROP_PERCENT": "کاهش صف فروش",
    "BUY_QUEUE_DISAPPEARED": "از بین رفتن صف خرید",
    "SELL_QUEUE_DISAPPEARED": "از بین رفتن صف فروش",
    "BUY_QUEUE_VOLUME_ABOVE": "حداقل حجم صف خرید",
    "SELL_QUEUE_VOLUME_ABOVE": "حداقل حجم صف فروش",
    "BUY_QUEUE_VOLUME_BELOW": "حجم صف خرید کمتر یا مساوی مقدار مشخص",
    "SELL_QUEUE_VOLUME_BELOW": "حجم صف فروش کمتر یا مساوی مقدار مشخص",
    "BUY_QUEUE_PEAK_DROP_PERCENT": "کاهش صف خرید نسبت به بیشترین حجم مشاهده‌شده",
    "SELL_QUEUE_PEAK_DROP_PERCENT": "کاهش صف فروش نسبت به بیشترین حجم مشاهده‌شده",
}
BASE_TYPES = tuple(LABELS)
LIFECYCLE_LABELS = {
    f"{side}_QUEUE_{kind}": f"{label} صف {persian}"
    for kind, label in (
        ("FORMED", "تشکیل"),
        ("WEAKENED", "ضعیف شدن"),
        ("CONFIRMED_DISAPPEARED", "جمع شدن"),
        ("REFORMED", "تشکیل دوباره"),
    )
    for side, persian in (("BUY", "خرید"), ("SELL", "فروش"))
}
LABELS.update(LIFECYCLE_LABELS)
PRIVATE_TYPES = tuple(LABELS)
ADMIN_TYPES = (
    *BASE_TYPES,
    "BUY_QUEUE_VOLUME_ABOVE",
    "SELL_QUEUE_VOLUME_ABOVE",
    *LIFECYCLE_LABELS,
)
FAMILIES = [
    ("هشدارهای رفتار صف", "familylife"),
    ("حجم صف به عدد مشخصی برسد", "familyvolume"),
    ("صف نسبت به بیشترین حجم کاهش یابد", "familypeak"),
    ("صف در مدت کوتاه سریع کاهش یابد", "familyrapid"),
]


def type_buttons(family: str, *, channel: bool) -> list[tuple[str, str]]:
    kinds = ADMIN_TYPES if channel else PRIVATE_TYPES
    life = {
        "formed": ("تشکیل صف جدید", "FORMED"),
        "weakened": ("ضعیف شدن صف", "WEAKENED"),
        "gone": ("جمع شدن صف", "CONFIRMED_DISAPPEARED"),
        "reformed": ("تشکیل دوباره صف", "REFORMED"),
    }
    if family == "life":
        return [(value[0], "family" + key) for key, value in life.items()]
    if family in life:
        return [
            (LABELS[k], f"type{i}")
            for i, k in enumerate(kinds)
            if k in LIFECYCLE_LABELS and k.split("_QUEUE_")[1] == life[family][1]
        ]
    if not family:
        # Keep the existing shortcuts and callback indices for older workflows.
        return FAMILIES + [
            (LABELS[t], f"type{i}") for i, t in enumerate(kinds[: 6 if channel else 4])
        ]
    indices = (
        (6, 7, 10, 11)
        if channel and family == "volume"
        else (4, 5, 6, 7)
        if family == "volume"
        else (8, 9)
        if family == "peak"
        else (0, 1)
    )
    return [
        (
            (
                "حجم صف خرید بیشتر یا مساوی مقدار مشخص"
                if kinds[i].startswith("BUY_")
                else "حجم صف فروش بیشتر یا مساوی مقدار مشخص"
            )
            if kinds[i].endswith("VOLUME_ABOVE")
            else LABELS[kinds[i]],
            f"type{i}",
        )
        for i in indices
    ]


PERMISSION_NOTICES = {
    "DESTINATION_INVALID": "تلگرام اطلاعات این مقصد را نپذیرفت. شناسه، عضویت بات و اطلاعات دسترسی گروه باید بررسی شوند؛ این خطا به‌تنهایی اشتباه بودن شناسه را ثابت نمی‌کند.",
    "DESTINATION_UNRESOLVED": "اطلاعات دسترسی این گروه در نشست فعلی پیدا نشد. عضویت و شناسهٔ گروه را بررسی کنید، یک دستور با حساب مدیر در همان گروه بفرستید و سپس «بررسی دوباره» را بزنید. پیشوند شناسه را حدسی تغییر ندهید.",
    "DESTINATION_INACCESSIBLE": "تلگرام دسترسی بات به این مقصد را رد کرده است. عضویت بات و درست بودن شناسهٔ گروه را بررسی کنید.",
    "ADMIN_REQUIRED": "تلگرام برای این بررسی دسترسی ادمین می‌خواهد. بات را در همین مقصد ادمین کنید.",
    "CANNOT_POST": "بررسی انجام شد، اما هویت مقصد یا مجوز ادمین و ارسال بات تأیید نشد.",
    "NETWORK": "بررسی مقصد به‌علت خطای اتصال یا پایان مهلت ناموفق بود. اتصال تلگرام و پروکسی را بررسی و دوباره تلاش کنید.",
    "RATE_LIMITED": "تلگرام درخواست‌ها را موقتاً محدود کرده است. کمی بعد دوباره بررسی کنید.",
    "SESSION_CONFLICT": "نشست تلگرام تداخل دارد. فقط یک نمونه با این توکن روی یک میزبان اجرا کنید.",
    "INVALID_RESPONSE": "پاسخ بررسی مقصد معتبر نبود. نسخهٔ برنامه و شناسهٔ مقصد را بررسی کنید.",
    "UNAVAILABLE": "بررسی مقصد ناموفق بود؛ علت مشخصی ثبت نشده است. شناسهٔ گروه و اتصال را بررسی کنید.",
}
LIVE_NOTICE = "در نسخهٔ فعلی، دادهٔ صف بازار قابل‌تأیید نیست و هشدار زنده صادر نمی‌شود. تنظیمات هشدارها ذخیره می‌شوند."
HELP = """راهنمای دستورهای خصوصی
«خودرو» در مثال‌ها نام یک نماد بورسی است؛ نام نماد دلخواهتان را جای آن بنویسید. هر دستور نمونه را در یک پیام جدا بفرستید.

شروع و تنظیم دریافت هشدارهای خصوصی:
/start

نمایش همین راهنما:
/help

جست‌وجوی نماد؛ سپس نتیجهٔ درست را از دکمه‌ها انتخاب کنید:
/add خودرو

فهرست نمادهایی که خودتان اضافه کرده‌اید:
/list

بررسی صف یک نماد از فهرست شخصی:
/queue خودرو

ساخت هشدار خصوصی، مرحله‌به‌مرحله با دکمه‌ها:
/alert

مشاهده، ویرایش، روشن/خاموش کردن یا حذف هشدارهای خصوصی:
/alerts

سابقهٔ هشدارهای خصوصی؛ هر صفحه ۱۰ مورد:
/history

حذف نماد و هشدارهای خصوصی آن، پس از تأیید:
/remove خودرو
برای حذف فقط یک هشدار، از دکمهٔ حذف در /alerts استفاده کنید.
روش متنی: به‌جای RULE_ID شناسهٔ هشدار را از جزئیات /alerts کپی کنید:
/remove rule RULE_ID

وضعیت کلی پایش بازار، نه فقط نمادهای شما:
/status

لغو فرایند جاری بدون حذف هشدارهای ذخیره‌شده:
/cancel

شروع پیشنهادی: افزودن نماد با /add، انتخاب نتیجه و سپس زدن دکمهٔ «ساخت هشدار برای این نماد»."""

PERCENT_PROMPT = "درصد کاهش حجم صف را بنویسید؛ عددی بزرگ‌تر از ۰ تا ۱۰۰، با حداکثر شش رقم اعشار.\nمثال: ۳۰ یعنی کاهش حجم صف از ۱۰۰٬۰۰۰ به ۷۰٬۰۰۰ سهم یا کمتر. فقط عدد ۳۰ را بفرستید؛ علامت ٪ لازم نیست."
WINDOW_PROMPT = "بازهٔ مقایسه را انتخاب کنید:\nمثلاً «۵ دقیقه» یعنی مقایسهٔ حجم فعلی صف با دادهٔ معتبر حدود ۵ دقیقه قبل. این گزینه فاصلهٔ ارسال پیام‌ها نیست."
TYPE_PROMPT = "نوع هشدار را انتخاب کنید:\n«کاهش صف» یعنی کم‌شدن حجم سفارش‌های صف؛ «از بین رفتن صف» یعنی پس از صف خرید یا فروش، وضعیت «بدون صف» با دادهٔ معتبر تأیید شود. صف مخالف به‌تنهایی نشانهٔ از بین رفتن صف نیست."


def lifecycle_prompt(confirmations: int) -> str:
    return (
        "هشدارهای رفتار صف\nچه تغییری را می‌خواهید دنبال کنید؟\n"
        f"تغییر باید در {fa(confirmations)} دادهٔ معتبرِ پشت‌سرهم دیده شود؛ خواندن دوبارهٔ همان داده حساب نمی‌شود.\n"
        "قطع دریافت داده به معنی تغییر صف نیست. پس از وصل‌شدن، بات ابتدا وضعیت صف را دوباره تأیید می‌کند."
    )


def hysteresis_prompt(kind: str) -> str:
    example = (
        "مثال: برای حد ۱ میلیون سهم و مقدار ۵، حجم باید ابتدا از ۱٬۰۵۰٬۰۰۰ سهم بیشتر شود تا رسیدن دوباره به حد هشدار بررسی شود."
        if kind.endswith("VOLUME_BELOW")
        else "مثال: برای حد ۱ میلیون سهم و مقدار ۵، حجم باید ابتدا از ۹۵۰٬۰۰۰ سهم کمتر شود تا رسیدن دوباره به حد هشدار بررسی شود. مقدار ۱۰۰ این هشدار را غیرقابل‌فعال‌شدن می‌کند."
        if kind.endswith("VOLUME_ABOVE")
        else "مثال: اگر هشدار برای افت ۳۰٪ باشد و اینجا ۵ بنویسید، افت باید به ۲۵٪ یا کمتر برگردد تا هشدار بعدی ممکن شود."
    )
    return (
        "صف چقدر برگردد تا هشدار دوباره آماده شود؟\n"
        "عددی از ۰ تا ۱۰۰ بنویسید؛ مقدار پیش‌فرض ۵ است.\n"
        + example
        + "\nزمان انتظار بین هشدارها هم باید تمام شده باشد."
    )


def first_step(kind: str, percent_step: str) -> str:
    if kind.endswith("WEAKENED"):
        return percent_step
    return (
        percent_step
        if kind.endswith("PEAK_DROP_PERCENT")
        else "window"
        if kind.endswith("DROP_PERCENT")
        else "minimum"
    )


def wizard_config(
    kind: str,
    percent: float | None,
    window: int | None,
    minimum: int | None,
    hysteresis: float = 5,
    legacy_volume: bool = False,
    disappearance_seconds: int = 0,
) -> dict[str, Any]:
    if kind in LIFECYCLE_LABELS:
        if kind.endswith("WEAKENED"):
            return dict(thresholdPercent=percent, minimumBaselineVolume=minimum)
        if kind.endswith("CONFIRMED_DISAPPEARED"):
            return dict(minimumPreviousVolume=minimum)
        if kind.endswith("REFORMED"):
            return dict(
                minimumVolume=minimum, minimumDisappearanceSeconds=disappearance_seconds
            )
        return dict(minimumVolume=minimum)
    if kind.endswith("PEAK_DROP_PERCENT"):
        return dict(
            thresholdPercent=percent,
            minimumPeakVolume=minimum,
            hysteresisPercent=hysteresis,
        )
    if kind.endswith("DROP_PERCENT"):
        return dict(
            thresholdPercent=percent,
            windowMinutes=window,
            minimumBaselineVolume=minimum,
        )
    if kind.endswith(("VOLUME_BELOW", "VOLUME_ABOVE")):
        return (
            dict(minimumVolume=minimum)
            if legacy_volume
            else dict(thresholdVolume=minimum, hysteresisPercent=hysteresis)
        )
    return dict(minimumPreviousVolume=minimum)


def minimum_prompt(kind: str) -> str:
    if kind in LIFECYCLE_LABELS:
        label = (
            "بیشترین حجم ثبت‌شدهٔ این صف، پیش از ضعیف‌شدن"
            if kind.endswith("WEAKENED")
            else "آخرین حجم صف، پیش از جمع‌شدن"
            if kind.endswith("DISAPPEARED")
            else "حجم صف هنگام تشکیل"
        )
        return f"{label} دست‌کم چند سهم باشد؟\nیک مقدار را انتخاب کنید یا عدد صحیح بنویسید. برای بررسی صف با هر حجمی، ۰ بفرستید. این شرط فقط هنگام تأیید تغییر بررسی می‌شود؛ بزرگ‌ترشدن صف در زمان بعد، هشدار قبلی را فعال نمی‌کند."
    if kind.endswith(("VOLUME_BELOW", "VOLUME_ABOVE")):
        return "مقدار موردنظر چند سهم باشد؟ عدد صحیح بزرگ‌تر از صفر بنویسید؛ مثلاً ۲٬۰۰۰٬۰۰۰. مقایسه شامل مقدار مساوی هم می‌شود. این مقدار حجم صف است، نه حجم معاملات."
    if kind.endswith("PEAK_DROP_PERCENT"):
        return "بیشترین حجم ثبت‌شدهٔ صف دست‌کم چند سهم باشد تا کاهش آن بررسی شود؟ برای بررسی صف با هر حجمی، ۰ بفرستید. ممکن است بخشی از داده‌های امروز را دریافت نکرده باشیم؛ این عدد لزوماً بیشترین حجم کل روز نیست."
    label = (
        "حجم صف در ابتدای بازهٔ مقایسه"
        if kind.endswith("DROP_PERCENT")
        else "حجم صف پیش از از بین رفتن"
        if kind.endswith("DISAPPEARED")
        else "حجم فعلی صف"
    )
    example = (
        "مثال: ۱۰۰۰۰۰ یعنی حجم فعلی صف دست‌کم ۱۰۰٬۰۰۰ سهم باشد. این عدد حجم صف است، نه حجم معاملات."
        if kind.endswith("VOLUME_ABOVE")
        else "مثال: ۱۰۰۰۰۰ یعنی فقط صفی بررسی شود که حجم قبلی آن دست‌کم ۱۰۰٬۰۰۰ سهم بوده است. برای کاهش درصدی، حجم قبلی همان حجم ابتدای بازه است."
    )
    return (
        f"حداقل {label} چند سهم باشد؟ فقط عدد صحیح بر حسب سهم بفرستید.\n{example}\n"
        + (
            "عدد باید بزرگ‌تر از صفر باشد."
            if kind.endswith("VOLUME_ABOVE")
            else "برای نداشتن فیلتر حداقل حجم، ۰ بفرستید."
        )
    )


def numeric_error(step: str, kind: str) -> str:
    if step == "duration":
        return "زمان نامعتبر است؛ عدد صحیحی از ۰ تا ۸۶۴۰۰ بر حسب ثانیه بنویسید."
    if step == "hysteresis":
        return "عدد نامعتبر است؛ عددی از ۰ تا ۱۰۰ با حداکثر شش رقم اعشار بنویسید."
    return (
        "درصد نامعتبر است؛ عددی بزرگ‌تر از ۰ و حداکثر ۱۰۰ با حداکثر شش رقم اعشار بنویسید؛ مثلاً ۳۰."
        if step in ("percent", "threshold")
        else "حجم نامعتبر است؛ عدد صحیح "
        + (
            "بزرگ‌تر از صفر"
            if kind.endswith(("VOLUME_ABOVE", "VOLUME_BELOW"))
            else "صفر یا بیشتر"
        )
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
    if isinstance(c, LifecycleConfig):
        if tree.type.endswith("WEAKENED"):
            return f"{label}: کاهش حداقل {fa(c.threshold_percent)}٪ از بیشترین حجم ثبت‌شدهٔ همین صف؛ حجم پیش از کاهش دست‌کم {fa(c.minimum_baseline_volume)} سهم"
        if tree.type.endswith("REFORMED"):
            return f"{label}؛ حجم جدید حداقل {fa(c.minimum_volume)} سهم؛ دست‌کم {fa(c.minimum_disappearance_seconds)} ثانیه پس از جمع شدن تأییدشده"
        return f"{label}؛ حجم {'قبلی' if tree.type.endswith('DISAPPEARED') else 'جدید'} حداقل {fa(c.minimum_volume)} سهم"
    if isinstance(c, ThresholdConfig):
        comparison = (
            "کمتر یا مساوی" if tree.type.endswith("BELOW") else "بیشتر یا مساوی"
        )
        return f"{label}: {comparison} {fa(c.threshold_volume)} سهم؛ بازگشت لازم برای آماده‌شدن دوباره: {fa(c.hysteresis_percent)}٪ از مقدار تعیین‌شده. پیش از اولین هشدار هم باید شرط بازگشت دیده شود."
    if isinstance(c, PeakConfig):
        return f"{label}: حداقل {fa(c.threshold_percent)}٪؛ بیشترین حجم مشاهده‌شده حداقل {fa(c.minimum_peak_volume)} سهم؛ برای آماده‌شدن دوباره، افت باید به {fa(max(0, c.threshold_percent - c.hysteresis_percent))}٪ یا کمتر برگردد."
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
        else "منتظر شروع ساعت پایش"
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
        else "روزها و ساعت‌های پایش تنظیم نشده‌اند"
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
            lines = [evidence_text(m, bool(payload.get("escalation"))) for m in matches]
            message = (
                f"هشدار ترکیبی بورس\nنماد: {symbol}\n"
                + "\nنتیجه: "
                + (
                    "همهٔ شرط‌های لازم برقرار هستند."
                    if payload["operator"] == "AND"
                    else "حداقل یکی از شرط‌ها برقرار است."
                )
                + "\nشرط‌های تأییدشده:\n"
                + "\n\n".join(lines)
                + f"\nزمان داده: {tehran_time(timestamp)} به وقت تهران"
            )
            if any(isinstance(m.condition.config, LifecycleConfig) for m in matches):
                if len(matches) == 1 and payload["operator"] is None:
                    message = f"هشدار {LABELS[matches[0].condition.type]}\nنماد: {symbol}\n{lines[0]}\nزمان داده: {tehran_time(timestamp)} به وقت تهران"
                message += "\nاین مشاهدهٔ رفتار صف است، نه توصیهٔ معامله."
        else:
            kind = payload["type"]
            if kind not in PRIVATE_TYPES:
                raise ValueError("Invalid event type")
            previous, current = (
                integer(payload["previousVolume"]),
                integer(payload["currentVolume"]),
            )
            percent = number(payload["actualPercent"], 0, 100)
            window = payload["windowMinutes"]
            if payload.get("escalation"):
                window = None
            if window is not None:
                number(window, -float("inf"))
            comparison = (
                "همان حجم مبنای هشدار اول"
                if payload.get("escalation")
                else "آخرین وضعیت تأییدشده"
                if window is None
                else fa(window) + " دقیقه"
            )
            message = f"هشدار {LABELS[kind]}\nنماد: {symbol}\nحجم قبلی صف: {quantity(previous)} سهم\nحجم فعلی صف: {quantity(current)} سهم\nمیزان کاهش: {fa(round(percent, 2))}٪\nبازهٔ مقایسه: {comparison}\nزمان داده: {tehran_time(timestamp)} به وقت تهران"
            if kind.endswith("PEAK_DROP_PERCENT"):
                message = f"هشدار {LABELS[kind]}\nنماد: {symbol}\nبیشترین حجم مشاهده‌شده: {quantity(previous)} سهم\nحجم فعلی صف: {quantity(current)} سهم\nکاهش نسبت به بیشترین حجم مشاهده‌شده: {fa(round(percent, 2))}٪\nممکن است همهٔ داده‌های امروز را نداشته باشیم؛ این عدد لزوماً بیشترین حجم کل روز نیست.\nزمان داده: {tehran_time(timestamp)} به وقت تهران"
            elif kind.endswith(("VOLUME_BELOW", "VOLUME_ABOVE")):
                condition = parse_tree(payload["definition"])
                message = f"هشدار حجم صف\nنماد: {symbol}\n{tree_text(condition)}\nحجم قبلی: {quantity(previous)} سهم\nحجم فعلی: {quantity(current)} سهم\nحجم صف به حد تعیین‌شده رسید.\nزمان داده: {tehran_time(timestamp)} به وقت تهران"
        title, rest = message.split("\n", 1)
        message = (
            title
            + "\nاهمیت: "
            + SEVERITY_LABELS.get(event.get("severity", "normal"), "عادی")
            + "\n"
            + rest
        )
        if payload.get("escalation") is True:
            message += (
                "\nافت صف بیشتر شده است؛ این پیام ادامهٔ هشدار قبلی است و با همان حجم مبنای هشدار اول مقایسه می‌شود.\nزمان هشدار اول: "
                + tehran_time(payload.get("initialTimestamp"))
            )
        for outcome in event.get("outcomes", []):
            message += f"\n\n{fa(outcome['window_minutes'])} دقیقه بعد: "
            if outcome["status"] == "pending":
                message += "منتظر دریافت داده در زمان تعیین‌شده"
            elif outcome["status"] != "completed":
                message += "دادهٔ معتبر موجود نیست"
            else:
                percent = outcome["queue_change_percent"]
                message += "تغییر حجم صف: " + (
                    "قابل‌محاسبه نیست"
                    if percent is None
                    else fa(round(percent, 2)) + "٪"
                )
                message += (
                    "\nتغییر قیمت معامله: دادهٔ معتبر موجود نیست"
                    if outcome["price_change_percent"] is None
                    else "\nتغییر قیمت معامله: "
                    + fa(round(outcome["price_change_percent"], 2))
                    + "٪"
                )
                message += "\nزمان مشاهده: " + tehran_time(outcome["observed_at"])
        message = limit_message(message, 3500)
        if event.get("ins_code") is not None:
            message += "\nصفحهٔ نماد:\n" + stock_url(event["ins_code"])
        return message
    except ValueError, TypeError, KeyError, OverflowError, OSError:
        return str(event.get("message") or "هشدار قدیمی")


def evidence_text(evidence: Evidence, escalation: bool = False) -> str:
    c = evidence.condition.config
    if isinstance(c, LifecycleConfig):
        message = LABELS[evidence.condition.type]
        if evidence.condition.type.endswith("CONFIRMED_DISAPPEARED"):
            message += f"\nآخرین حجم معتبر صف: {quantity(evidence.previous_volume)} سهم\nزمان آخرین صف معتبر: {tehran_time((evidence.details or {}).get('previous_timestamp'))}\nوضعیت تأییدشدهٔ فعلی: بدون صف"
        elif evidence.condition.type.endswith("WEAKENED"):
            message += f"\nحجمی که کاهش نسبت به آن حساب شده: {quantity(evidence.previous_volume)} سهم\nحجم فعلی: {quantity(evidence.current_volume)} سهم\nمیزان کاهش: {fa(round(evidence.actual_percent, 2))}٪\nممکن است همهٔ داده‌های امروز را نداشته باشیم؛ این عدد لزوماً بیشترین حجم کل روز نیست."
        else:
            message += f"\nحجم صف جدید: {quantity(evidence.current_volume)} سهم"
            if evidence.condition.type.endswith("REFORMED"):
                message += f"\nزمان جمع شدن تأییدشدهٔ قبلی: {tehran_time((evidence.details or {}).get('disappeared_at'))}\nزمان مشاهدهٔ تشکیل دوباره: {tehran_time((evidence.details or {}).get('timestamp'))}"
        return message
    label = (
        LABELS[evidence.condition.type] if escalation else tree_text(evidence.condition)
    )
    if isinstance(c, VolumeConfig):
        return f"{label}\nحجم فعلی صف: {fa(evidence.current_volume)} سهم"
    previous = "بیشترین حجم مشاهده‌شده" if isinstance(c, PeakConfig) else "حجم قبلی"
    message = f"{label}\n{previous}: {quantity(evidence.previous_volume)}؛ حجم فعلی: {quantity(evidence.current_volume)} سهم"
    if not isinstance(c, ThresholdConfig):
        message += f"\nمیزان کاهش: {fa(round(evidence.actual_percent, 2))}٪"
    if isinstance(c, PeakConfig):
        message += "\nممکن است همهٔ داده‌های امروز را نداشته باشیم؛ این عدد لزوماً بیشترین حجم کل روز نیست."
    return message


def quantity(value: int) -> str:
    for divisor, label in ((1_000_000_000, "میلیارد"), (1_000_000, "میلیون")):
        if value >= divisor:
            return f"{fa(round(value / divisor, 2))} {label} ({fa(value)})"
    return fa(value)


def stock_url(ins_code: object) -> str:
    return "https://tsetmc.com/instInfo/" + code(ins_code)


def limit_message(value: str, units: int = 4096) -> str:
    raw = value.encode("utf-16-le")
    return (
        value
        if len(raw) <= units * 2
        else raw[: (units - 1) * 2].decode("utf-16-le", errors="ignore") + "…"
    )


def statistics_text(stats: dict[str, Any]) -> str:
    counts = {row["severity"]: row["count"] for row in stats["alerts"]}
    lines = [
        "آمار هشدارهای مقصد مشترک در ۳۰ روز اخیر",
        f"تعداد هشدارها: {fa(sum(counts.values()))}",
    ]
    lines.extend(
        f"هشدارهای {label}: {fa(counts.get(severity, 0))}"
        for severity, label in SEVERITY_LABELS.items()
    )
    lines.append(
        f"دفعات ادامهٔ شرایط هشدار بدون ارسال پیام تکراری: {fa(stats['suppressed'])}"
    )
    for window in (5, 15, 30):
        group = [row for row in stats["outcomes"] if row["window_minutes"] == window]
        completed: dict[str, Any] = next(
            (row for row in group if row["status"] == "completed"), {}
        )
        missing = sum(
            row["count"]
            for row in group
            if row["status"] in ("insufficient_data", "expired")
        )
        pending = sum(row["count"] for row in group if row["status"] == "pending")
        average = completed.get("price_average")
        lines.append(
            f"\n{fa(window)} دقیقه پس از هشدار: {fa(completed.get('count', 0))} مورد با دادهٔ صف؛ {fa(missing)} مورد بدون دادهٔ معتبر؛ {fa(pending)} مورد در انتظار داده"
        )
        lines.append(
            "میانگین تغییر قیمت: "
            + (
                "دادهٔ کافی موجود نیست"
                if average is None
                else fa(round(average, 2))
                + f"٪؛ {fa(completed['price_samples'])} نمونه"
            )
        )
    lines.append(
        "این آمار فقط وضعیت ثبت‌شدهٔ صف‌ها را نشان می‌دهد؛ از آن نمی‌توان سودآوری معامله را نتیجه گرفت."
    )
    return "\n".join(lines)
