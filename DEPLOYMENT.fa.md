# استقرار نسخهٔ پایتون روی VPS

پروژهٔ اصلی اکنون پایتون است و از ریشه ساخته می‌شود. این دستورها برای اجرای دستی اپراتور روی VPS هستند؛ در توسعه هیچ اتصال به VPS، استقرار واقعی یا اعلان زنده انجام نشده است. کد Node حذف شده، اما SQL و journal اصلی بدون تغییر در `migrations/` حفظ شده‌اند.

## پیش‌نیاز و تنظیمات

روی Ubuntu، Git و Docker Engine همراه با Compose نصب کنید؛ مراحل نصب را از [راهنمای رسمی Docker](https://docs.docker.com/engine/install/ubuntu/) دنبال کنید. فرمان‌ها دسترسی کاربر جاری به Docker را فرض می‌کنند. Python و Node روی میزبان لازم نیستند؛ Python ۳.۱۴ داخل تصویر است.

فقط یک نمونه با یک token روی یک میزبان اجرا شود. نام پروژهٔ Compose، مسیر دیتابیس و volume نصب موجود را حفظ کنید. پیش‌فرض این راهنما `stock-alert-bot` است؛ اگر نصب فعلی نام دیگری دارد، همان را جایگزین کنید. قفل SQLite از مسیر مشترک محافظت می‌کند؛ جلوی اجرای بات با همان token روی میزبان دیگر را نمی‌گیرد.

BestLimits هنوز زمان منبع معتبر و قرارداد کامل بودن ندارد؛ دادهٔ آن تاییدنشده باقی می‌ماند و هشدار زنده تولید نمی‌کند. تنظیم نشست این محدودیت را رفع نمی‌کند؛ [مشاهدات TSETMC](docs/TSETMC.md) را بخوانید.

در `.env` این موارد را تنظیم کنید:

- `TELEGRAM_BOT_TOKEN` و شناسه‌های عددی مجاز در `TELEGRAM_ALLOWED_USER_IDS`، جداشده با ویرگول.
- `TELEGRAM_API_ID` و `TELEGRAM_API_HASH` طبق [راهنمای رسمی Telethon](https://docs.telethon.dev/en/stable/basic/signing-in.html). خود برنامه فقط با token بات وارد می‌شود؛ شماره، کد یا رمز حساب شخصی نمی‌گیرد.
- `NODE_ENV=production`؛ نام متغیر برای سازگاری حفظ شده است.
- `DATABASE_URL=/app/data/stock-alert.db`؛ در نصب موجود همان مسیر واقعی قبلی را حفظ کنید. مسیر حافظه‌ای یا خارج از `/app/data` در تصویر رد می‌شود.
- `MARKET_MONITOR_FORCE_ACTIVE=false` و بدون `DEBUG`. نشست‌ها و تعطیلی‌های تاییدشده به وقت تهران؛ مقدار پیش‌فرض `MARKET_SESSIONS_JSON=[]` یعنی بسته.
- برای کانال، هم `TELEGRAM_ADMIN_USER_IDS` و هم `TELEGRAM_CHANNEL_ID`؛ برای حالت خصوصی هر دو خالی باشند. مدیرها باید در فهرست مجاز باشند و ابتدا در خصوصی با بات تعامل کنند. بات در کانال باید administrator با اجازهٔ ارسال باشد. مشترک‌های کانال برای مسیریابی خوانده نمی‌شوند.

## نصب تازه

برای نصب فعال Node ابتدا بخش انتقال را بخوانید. روی سرور تازه:

```bash
sudo install -d -o "$USER" -g "$(id -gn)" /opt/stock-alert-bot
git clone --branch codex/python-primary https://github.com/pooria920/trade-alert.git /opt/stock-alert-bot
cd /opt/stock-alert-bot
git rev-parse HEAD
cp .env.example .env
chmod 600 .env
${EDITOR:-nano} .env
```

برنچ بالا محل این تغییر است؛ پس از ادغام، انتشار بازبینی‌شده را انتخاب کنید. برای مخزن خصوصی از احراز هویت Git خودتان استفاده کنید؛ token را در URL نگذارید.

در هر نشست Bash روی سرور، از ریشهٔ پروژه تابع زیر را تعریف کنید؛ برای نصب موجود نام پروژه را عوض نکنید:

```bash
set -euo pipefail
cd /opt/stock-alert-bot
export COMPOSE_PROJECT_NAME=stock-alert-bot
dc() { docker compose --project-name "$COMPOSE_PROJECT_NAME" --project-directory "$PWD" --env-file .env -f compose.yaml "$@"; }
dc config --quiet
```

خروجی کامل `compose config` یا محیط کانتینر را منتشر نکنید؛ ممکن است اسرار را نمایش دهد. تصویر با کاربر `node` و UID/GID برابر ۱۰۰۰ اجرا می‌شود تا volume قبلی سازگار بماند؛ این نام به معنی وجود Node در تصویر نیست.

پس از تنظیم محیط:

```bash
dc build bot
dc run --rm --no-deps -T bot python -c 'import os; from trade_alert.config import parse_env; parse_env(os.environ); print("Configuration valid")'
dc run --rm --no-deps -T bot python -m trade_alert.maintenance migrate /app/data/stock-alert.db
dc up -d --no-deps --scale bot=1 bot
dc ps bot
dc logs --tail=100 bot
```

اگر مسیر دیتابیس متفاوت است، آن را در تمام فرمان‌ها جایگزین کنید. اعتبارسنجی تنظیمات و migration آفلاین هستند؛ شروع واقعی به تلگرام و درنشست فعال با watchlist به بازار وصل می‌شود. مهاجرت دستی نیازمند توقف بات است و با همان قفل برنامه اجرا می‌شود.

## انتقال نصب فعلی از Node

پیش از checkout جدید، در پوشهٔ قدیمی همان نام پروژه و تابع `dc` بالا را تنظیم کنید. ابتدا تصویر فعلی را نگه دارید و با ابزار قدیمی بکاپ آنلاین بگیرید؛ بکاپ باید پیش از ساخت، توقف و migration باشد:

```bash
OLD_CONTAINER="$(dc ps -q bot)"
test -n "$OLD_CONTAINER"
OLD_IMAGE="$(docker inspect --format '{{.Image}}' "$OLD_CONTAINER")"
docker image tag "$OLD_IMAGE" "tse-stock-alert-bot:before-python-$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -d -m 0700 -o 1000 -g 1000 /opt/stock-alert-backups
bash scripts/backup-db.sh /opt/stock-alert-backups /app/data/stock-alert.db
```

اسکریپت آخر متعلق به checkout قدیمی است؛ قبل از تعویض کد اجرا شود. سلامت SQLite و foreign keys در بکاپ بررسی می‌شود. مسیر فایل چاپ‌شده و موفقیت فرمان را کنترل کنید. اگر اسکریپت قدیمی موجود نیست، همان ابزار داخل تصویر فعلی را اجرا کنید:

```bash
dc run --rm --no-deps -T -v /opt/stock-alert-backups:/backups bot node dist/database/backup.js backup /app/data/stock-alert.db /backups
```

اکنون انتشار پایتون را انتخاب و API ID/hash را به محیط اضافه کنید. تغییر محلی را پیش از checkout بررسی کنید؛ reset اجباری نزنید:

```bash
git fetch origin
git switch --track origin/codex/python-primary
git rev-parse HEAD
${EDITOR:-nano} .env
dc config --quiet
dc build bot
dc run --rm --no-deps -T bot python -c 'import os; from trade_alert.config import parse_env; parse_env(os.environ); print("Configuration valid")'
read -r -p 'Stop Node and start the reviewed Python release? Type YES: ' CONFIRM
test "$CONFIRM" = YES
dc stop bot
dc run --rm --no-deps -T bot python -m trade_alert.maintenance migrate /app/data/stock-alert.db
dc up -d --no-deps --scale bot=1 bot
dc ps bot
dc logs --tail=100 bot
```

اگر برنچ از قبل محلی است، از `git switch codex/python-primary` و سپس `git merge --ff-only origin/codex/python-primary` استفاده کنید. شکست هر گام باید ادامه را متوقف کند؛ rollback خودکار وجود ندارد. پس از شکست migration یا شروع، بات را متوقف نگه دارید و علت را بررسی کنید.

Node و پایتون هم‌زمان با یک token اجرا نشوند. کاربران، ترجیحات، watchlist، قوانین، رخدادها و outbox حفظ می‌شوند. مالک NULL و دادهٔ بدون منشا همچنان قرنطینه‌اند. رویدادهای زمان خاموشی لزوماً بازپخش نمی‌شوند. تحویل حداقل یک‌باره است و پیام پذیرفته‌شده‌ای که موفقیتش ثبت نشده، ممکن است تکرار شود.

## سلامت و توقف

healthcheck تصویر ۶۰ ثانیه فرصت شروع دارد. پس از شروع، سلامت و لاگ‌ها را بررسی کنید:

```bash
dc exec -T bot python -m trade_alert.health
dc ps bot
dc logs --tail=100 bot
```

سلامت به heartbeat تازه، پیشرفت مستقل monitor، نوشتن SQLite و پاسخ اخیر MTProto نیاز دارد. `CONNECTED` وضعیت اتصال تلگرام است؛ بازار بسته، فهرست خالی یا دادهٔ تاییدنشده می‌تواند با فرایند سالم همراه باشد. شبکهٔ واقعی VPS در توسعه بررسی نشده است. برای بررسی مجوز کانال پیام آزمایشی نفرستید؛ برنامه هویت کانال و مجوز خود بات را می‌سنجد.

```bash
dc stop bot
```

SIGTERM همهٔ workerها و درخواست‌ها را لغو و join می‌کند، سپس SQLite را می‌بندد و قفل را آزاد می‌کند. مهلت برنامه ۱۵ ثانیه و Compose برابر ۲۵ ثانیه است. فایل قفل، DB/WAL/SHM یا volume را برای حل خطا حذف نکنید.

## بکاپ و به‌روزرسانی پایتون

پوشهٔ بکاپ روی میزبان، خارج از volume و قابل‌نوشتن برای UID ۱۰۰۰ باشد:

```bash
sudo install -d -m 0700 -o 1000 -g 1000 /opt/stock-alert-backups
dc run --rm --no-deps -T -v /opt/stock-alert-backups:/backups bot python -m trade_alert.maintenance backup /app/data/stock-alert.db /backups
```

بکاپ آنلاین شامل WAL ثبت‌شده است؛ integrity و foreign keys کنترل و فایل مستقل با DELETE journal نهایی می‌شود. نام شامل هش مسیر، زمان UTC و UUID است. فایل زنده را ساده کپی نکنید. بکاپ‌های مهم را با روش مورداعتماد خارج از میزبان نگهداری کنید. حذف قدیمی‌ها فقط با تایید صریح و محدود به فایل‌های تولیدشدهٔ همین منبع است:

```bash
dc run --rm --no-deps -T -v /opt/stock-alert-backups:/backups bot python -m trade_alert.maintenance backup /app/data/stock-alert.db /backups --days 14 --confirm-prune
```

برای انتشار بعدی، ابتدا با تصویر پایتون فعلی بکاپ بگیرید؛ سپس commit بازبینی‌شده را checkout کنید، تصویر را بسازید و تنظیمات را اعتبارسنجی کنید. با تایید صریح اپراتور فقط `bot` را متوقف کنید، migration را اجرا و همان سرویس را با یک نمونه شروع کنید. ترتیب همان انتقال اولیه است؛ فرمان‌های Node دیگر لازم نیستند.

اگر میزبان Python ۳.۱۴ و وابستگی‌ها را دارد، helper موجود همین ترتیب را اجرا می‌کند:

```bash
python -m trade_alert.operations deploy /opt/stock-alert-backups /app/data/stock-alert.db --confirm-deploy
```

helper به تصویر پایتون موجود نیاز دارد و ابزار اولین انتقال از Node نیست. فقط daemon محلی را می‌پذیرد؛ fetch کد، rollback، حذف volume یا تغییر سرویس نامرتبط انجام نمی‌دهد.

## بازیابی به مسیر جدید

بکاپ را صریح انتخاب و بات را متوقف کنید. هدف باید جدید و فاقد DB/WAL/SHM باشد:

```bash
dc stop bot
dc run --rm --no-deps -T -v /opt/stock-alert-backups:/restore:ro bot python -m trade_alert.maintenance restore /restore/CHOSEN_BACKUP.db /app/data/stock-alert-restored.db --confirm-restore
```

نام نمونه را با فایل واقعی جایگزین کنید. نتیجه را بررسی و `DATABASE_URL` را به مسیر جدید تغییر دهید؛ سپس migration روی همان مسیر و شروع تک‌نمونه فقط با تصمیم اپراتور انجام شود. بازیابی هیچ فایل موجودی را بازنویسی و بات را خودکار شروع نمی‌کند. برای برگشت به تصویر قبل، سازگاری schema و outbox را بررسی کنید؛ بکاپ قدیمی ممکن است پیام تکراری بسازد. هرگز `docker compose down -v` یا حذف volume را برای به‌روزرسانی اجرا نکنید.

## آزمون انتشار

دستورهای تست در [README.md](README.md) هستند. از ریشه `docker build -t tse-stock-alert-bot:local .` و سپس `python tests/smoke_image.py` را اجرا کنید. آزمون فقط کانتینر با شبکهٔ قطع‌شده، volume موقت UUID و دادهٔ ساختگی دارد؛ سلامت، SIGTERM، قفل و بکاپ/بازیابی را می‌سنجد. معماری آزمون محلی `linux/amd64` است؛ معماری مقصد را جدا بسازید و بررسی کنید. Compose نام تصویر، سرویس و volume قبلی، محدودیت منابع، فایل‌سیستم فقط‌خواندنی و نبود پورت عمومی را حفظ می‌کند.
