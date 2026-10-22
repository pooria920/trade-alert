# نصب اولیهٔ بات پایتون روی VPS

این راهنما از یک نصب تازه روی Ubuntu شروع می‌کند. بات با Python ۳.۱۴ و Telethon داخل Docker اجرا می‌شود. روی میزبان Git و Docker Engine همراه با Compose لازم است؛ مراحل نصب را از [راهنمای رسمی Docker](https://docs.docker.com/engine/install/ubuntu/) دنبال و دسترسی کاربر جاری به Docker را تنظیم کنید. بات وب‌سرور و پورت ورودی ندارد.

## ۱. دریافت پروژه

```bash
sudo install -d -o "$USER" -g "$(id -gn)" /opt/stock-alert-bot
git clone --branch codex/telegram-groups https://github.com/pooria920/trade-alert.git /opt/stock-alert-bot
cd /opt/stock-alert-bot
git rev-parse HEAD
cp .env.example .env
chmod 600 .env
${EDITOR:-nano} .env
```

برنچ بالا شامل قابلیت گروه و پروکسی است؛ پس از ادغام، انتشار بازبینی‌شدهٔ `main` را انتخاب کنید. برای مخزن خصوصی از احراز هویت Git خودتان استفاده کنید؛ token را در URL نگذارید.

## ۲. تنظیم محیط

در `.env` این موارد را کامل کنید:

- `TELEGRAM_BOT_TOKEN`: توکن بات از BotFather.
- `TELEGRAM_API_ID` و `TELEGRAM_API_HASH`: مشخصات برنامهٔ تلگرام طبق [راهنمای Telethon](https://docs.telethon.dev/en/stable/basic/signing-in.html). خود بات با token وارد می‌شود؛ برنامه شماره، کد یا رمز حساب شخصی نمی‌گیرد.
- `TELEGRAM_ALLOWED_USER_IDS`: شناسه‌های عددی کاربران مجاز، جداشده با ویرگول.
- `NODE_ENV=production`: نام متغیر برای سازگاری حفظ شده است.
- `DATABASE_URL=/app/data/stock-alert.db`: دیتابیس در volume پایدار Docker ساخته می‌شود.
- `MARKET_MONITOR_FORCE_ACTIVE=false` و بدون `DEBUG`.

فایل `.env` را در Git نگذارید و خروجی کامل محیط یا `docker compose config` را منتشر نکنید.

### پروکسی تلگرام از سرور ایران

لینکی که فرستادید از نوع MTProto با Fake‑TLS و پیشوند `ee` است. **همان لینک کامل** را روی سرور در متغیر زیر بگذارید؛ این مثال فقط قالب است:

```dotenv
TELEGRAM_MTPROXY_URL="https://t.me/proxy?server=PROXY_HOST&port=443&secret=FULL_SECRET"
```

`PROXY_HOST` و `FULL_SECRET` را با مقدار واقعی جایگزین کنید. لینک `tg://proxy?...` هم پذیرفته می‌شود. پیشوند `ee` و ادامهٔ secret شامل دامنه را حذف نکنید. secretهای hex معمولی و `dd` هم پشتیبانی می‌شوند؛ دامنهٔ داخل secret نوع Fake‑TLS حداکثر ۲۲۰ بایت است. لینک واقعی شما در کد و این راهنما ذخیره نشده است.

پروکسی برای اتصال، ورود، دریافت دستور و ارسال پیام تلگرام استفاده می‌شود؛ درخواست‌های بازار از مسیر خود سرور می‌روند. به `HTTP_PROXY` یا `HTTPS_PROXY` نیازی نیست. اگر پروکسی قطع باشد، بات اتصال مستقیم را جایگزین نمی‌کند و سلامت آن تا دریافت پاسخ‌های تلگرام تایید نمی‌شود. خالی گذاشتن متغیر یعنی اتصال مستقیم.

Fake‑TLS با یک لایه روی انتقال رمزگذاری‌شدهٔ Telethon پیاده شده است؛ [پروکسی داخلی Telethon](https://docs.telethon.dev/en/stable/basic/signing-in.html#using-mtproto-proxies) به‌تنهایی این نوع را پوشش نمی‌دهد. آزمون‌ها آفلاین و با پاسخ ساختگی بوده‌اند؛ دسترسی این پروکسی از VPS ایران بررسی نشده است.

### افزودن بات به گروه یا کانال

برای حالت خصوصی، `TELEGRAM_ADMIN_USER_IDS` و `TELEGRAM_CHANNEL_ID` را خالی بگذارید. برای اجرای دستور و ارسال هشدار در گروه:

۱. در صورت غیرفعال بودن افزودن بات به گروه، با `/setjoingroups` در BotFather آن را فعال کنید؛ [تنظیمات رسمی بات](https://core.telegram.org/bots/features#edit-settings).
۲. بات را به گروه معمولی یا سوپرگروه اضافه و administrator کنید.
۳. شناسهٔ عددی منفی همان گروه را در `TELEGRAM_CHANNEL_ID` بگذارید. برای گروه معمولی پیشوند `-100` را دستی اضافه نکنید.
۴. شناسهٔ شخصی مدیرهای مجاز را در هر دو فهرست `TELEGRAM_ALLOWED_USER_IDS` و `TELEGRAM_ADMIN_USER_IDS` وارد کنید.
۵. پس از شروع بات، هر مدیر یک بار در خصوصی `/start` بفرستد؛ سپس در گروه با حساب شخصی خودش `/admin` یا `/admin@BotUsername` را اجرا کند. مدیر ناشناس پذیرفته نمی‌شود.

`/alert` ساخت هشدار و `/alerts` فهرست هشدارهای گروه را باز می‌کند. `/status`، `/cancel`، `/start` و `/help` هم پشتیبانی می‌شوند. متن هر مرحله را با Reply به آخرین پیام همان مرحله بفرستید. اطلاعات شخصی، watchlist و جزئیات خطای ارسال در خصوصی باقی می‌مانند. سایر اعضای گروه اجازهٔ مدیریت بات ندارند و ثبت یا فهرست نمی‌شوند.

فقط یک مقصد مشترک تنظیم می‌شود: گروه یا کانال. برای کانال هم همین متغیرها را تنظیم کنید؛ بات باید administrator با اجازهٔ ارسال باشد. برنامه هویت مقصد و مجوز خود بات را بدون پیام آزمایشی بررسی می‌کند. پس از تبدیل گروه به سوپرگروه، شناسهٔ جدید را صریح تنظیم کنید.

### ساعت و دادهٔ بازار

پیش‌فرض `MARKET_SESSIONS_JSON=[]` یعنی بازار بسته است. نشست‌ها و تعطیلی‌های تاییدشده را به وقت تهران تنظیم کنید. **BestLimits هنوز زمان منبع و قرارداد کامل بودن معتبر ندارد؛ دادهٔ فعلی آن تاییدنشده است و هشدار زنده تولید نمی‌کند.** تنظیم نشست این محدودیت را رفع نمی‌کند؛ [مشاهدات TSETMC](docs/TSETMC.md) را بخوانید.

## ۳. ساخت و اجرا

در هر نشست Bash از ریشهٔ پروژه، تابع زیر را تعریف کنید:

```bash
set -euo pipefail
cd /opt/stock-alert-bot
export COMPOSE_PROJECT_NAME=stock-alert-bot
dc() { docker compose --project-name "$COMPOSE_PROJECT_NAME" --project-directory "$PWD" --env-file .env -f compose.yaml "$@"; }
dc config --quiet
dc build bot
dc run --rm --no-deps -T bot python -c 'import os; from trade_alert.config import parse_env; parse_env(os.environ); print("Configuration valid")'
dc run --rm --no-deps -T bot python -m trade_alert.maintenance migrate /app/data/stock-alert.db
dc up -d --no-deps --scale bot=1 bot
dc ps bot
dc logs --tail=100 bot
```

اعتبارسنجی تنظیمات و ساخت دیتابیس آفلاین هستند و سلامت پروکسی را نمی‌سنجند. شروع بات به تلگرام و هنگام نشست فعال با watchlist به بازار وصل می‌شود. اگر فرمانی شکست خورد، ادامه ندهید و علت را بررسی کنید.

Compose خودش volume با نام `stock-alert-bot_stock-alert-data` را می‌سازد. تصویر با کاربر غیرریشهٔ `node` و UID/GID برابر ۱۰۰۰ اجرا می‌شود؛ این نام کاربر است و Node داخل تصویر نصب نیست. فقط مسیر داده و فضای موقت قابل‌نوشتن‌اند.

فقط یک نمونه با این token روی یک میزبان اجرا کنید. قفل SQLite از مسیر مشترک محافظت می‌کند؛ جلوی اجرای token روی میزبان دیگر را نمی‌گیرد. نام پروژهٔ Compose را بعد از نصب ثابت نگه دارید.

## ۴. بررسی سلامت

healthcheck تا ۶۰ ثانیه فرصت شروع دارد؛ سپس بررسی کنید:

```bash
dc exec -T bot python -m trade_alert.health
dc ps bot
dc logs --tail=100 bot
```

سلامت به heartbeat تازه، پیشرفت مستقل monitor، نوشتن SQLite و پاسخ اخیر MTProto نیاز دارد. `CONNECTED` وضعیت اتصال تلگرام است. بازار بسته، watchlist خالی یا دادهٔ تاییدنشده می‌تواند با فرایند سالم همراه باشد.

اگر اتصال برقرار نشد، کامل بودن لینک، دسترسی آدرس و پورت پروکسی از خود VPS، token و API ID/hash را بررسی کنید. لینک پروکسی یا `.env` را در گزارش خطا منتشر نکنید. برای اعمال تغییر `.env`:

```bash
dc up -d --no-deps --force-recreate --scale bot=1 bot
```

برای توقف از `dc stop bot` استفاده کنید. SIGTERM کارها را لغو و join می‌کند، SQLite را می‌بندد و قفل را آزاد می‌کند. مهلت برنامه ۱۵ ثانیه و Compose برابر ۲۵ ثانیه است. فایل قفل یا DB/WAL/SHM را برای حل خطا حذف نکنید.

## ۵. نگهداری داده پس از نصب

بکاپ آنلاین را خارج از volume بگیرید:

```bash
sudo install -d -m 0700 -o 1000 -g 1000 /opt/stock-alert-backups
dc run --rm --no-deps -T -v /opt/stock-alert-backups:/backups bot python -m trade_alert.maintenance backup /app/data/stock-alert.db /backups
```

فرمان، سلامت SQLite و foreign keys را بررسی و بکاپ مستقل می‌سازد. فایل زنده را ساده کپی نکنید؛ بکاپ‌های مهم را خارج از میزبان هم نگه دارید. پیش از به‌روزرسانی بعدی بکاپ بگیرید، تصویر را بسازید و تنظیمات را اعتبارسنجی کنید؛ سپس فقط `bot` را متوقف کنید، migration را با همان فرمان بالا اجرا و یک نمونه را شروع کنید. مهاجرت دستی به توقف بات نیاز دارد و قفل برنامه را می‌گیرد.

بازیابی فقط به مسیر جدید انجام می‌شود؛ نام نمونه را با فایل واقعی بکاپ جایگزین کنید:

```bash
dc stop bot
dc run --rm --no-deps -T -v /opt/stock-alert-backups:/restore:ro bot python -m trade_alert.maintenance restore /restore/CHOSEN_BACKUP.db /app/data/stock-alert-restored.db --confirm-restore
```

پس از بررسی نتیجه، `DATABASE_URL` را به مسیر جدید تغییر دهید، migration را روی همان مسیر اجرا و بات را شروع کنید. بازیابی چیزی را بازنویسی یا بات را خودکار شروع نمی‌کند. هرگز برای به‌روزرسانی `docker compose down -v` اجرا یا volume را حذف نکنید. تحویل پیام حداقل یک‌باره است؛ پس از قطع ناگهانی ممکن است پیام پذیرفته‌شده دوباره ارسال شود.

## آزمون نسخه در توسعه

دستورهای آزمون در [README.md](README.md) هستند. `docker build -t tse-stock-alert-bot:local .` و سپس `python tests/smoke_image.py` تصویر را با شبکهٔ قطع‌شده و دادهٔ موقت بررسی می‌کنند. آزمون پروکسی هم پاسخ‌های ساختگی دارد و به پروکسی واقعی وصل نمی‌شود. معماری محلی آزمون `linux/amd64` است؛ معماری مقصد را جدا بررسی کنید. در توسعه هیچ اتصال به VPS، استقرار واقعی یا اعلان زنده انجام نشده است.
