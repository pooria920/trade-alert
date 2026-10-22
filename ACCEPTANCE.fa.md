> وضعیت جاری: پروژه در ریشه است و Node حذف شده است. مقایسه‌های TypeScript این سند اکنون با خروجی ثابت ثبت‌شده پیش از حذف انجام می‌شوند؛ اجرای Node جزو تست‌ها نیست. مسیر `python/` در سابقهٔ قدیمی دیگر مسیر اجرایی نیست. راهنمای جاری: [DEPLOYMENT.md](DEPLOYMENT.md).

# پوشش پذیرش مهاجرت

محدودهٔ پذیرش، انتقال رفتار موجود و آماده‌سازی تولید تا Phase ۶A است. این سند تایید استقرار، سلامت سرویس‌های واقعی یا معتبر بودن دادهٔ BestLimits نیست. ورودی تاییدشده در تست‌ها ساختگی است؛ fixtureهای ثبت‌شدهٔ API تاییدنشده باقی می‌مانند.

| رفتار یا الزام                                                                       | پیاده‌سازی پایتون                         | بررسی متناظر                                                  |
| ------------------------------------------------------------------------------------ | ----------------------------------------- | ------------------------------------------------------------- |
| اعتبارسنجی snapshot و ارزیاب افت/ناپدیدشدن خرید و فروش، مرز درصد و بازیابی           | `evaluators.py`                           | `test_evaluators.py`، `test_parity.py`                        |
| تعریف JSON معتبر، AND/OR، سقف درخت و شروط، UNKNOWN و شواهد بازیابی                   | `definitions.py`، `composite.py`          | `test_parity.py`، `test_persistence.py`                       |
| بازار، حدود همان روز تهران، ترتیب کتاب، تازگی و رد دادهٔ نامعتبر                     | `market.py`، `tsetmc.py`                  | `test_market_client.py`، `test_market_parity.py`              |
| BestLimits بدون زمان منبع، کامل بودن نامعلوم و verified=false                        | `tsetmc.py`                               | fixtureهای اصلی و `test_market_client.py`                     |
| تنظیمات، زیرمجموعه بودن مدیرها، نشست صریح، محدودیت تولید و مسیر تصویر                | `config.py`، `schedule.py`                | `test_config.py`، `test_parity.py`، `container_smoke.py`      |
| migration و journal اصلی، WAL/FK/busy timeout، قفل پیش از شروع                       | `database.py`، `maintenance.py`           | `test_database.py`، `test_backup.py`، `container_smoke.py`    |
| قرنطینهٔ مالک NULL و منشا ناشناخته؛ cascade محدود به همان قانون                      | `users.py`، `channels.py`، `snapshots.py` | `test_database.py`، `test_services.py`، `test_persistence.py` |
| مجوز پیش از ثبت و handler، پیام/callback خصوصی منطبق و ترجیح شخصی                    | `bot.py`، `telegram.py`، `users.py`       | `test_bot.py`، `test_services.py`                             |
| جداسازی مالکیت خصوصی و کانال، پذیرش توسط مدیر فعلی، تعلیق پس از لغو دسترسی           | `alerts.py`، `channels.py`                | `test_services.py`، `test_delivery.py`                        |
| پیش‌نویس ده‌دقیقه‌ای، توکن تازه، revision، لغو، Back و تأیید تکراری                  | `bot.py`، `admin.py`                      | `test_bot.py`                                                 |
| بررسی هویت دقیق کانال و بات و مجوز administrator/posting                             | `telegram.py`، `channels.py`              | `test_bot.py`، `test_delivery.py`                             |
| تاریخچهٔ at-or-before محدود و هم‌نشست؛ زمان منبع جدا از دریافت                       | `snapshots.py`                            | `test_persistence.py`                                         |
| priming شروع/ویرایش/نشست، رد قدیمی/تکراری، baseline ثابت و تراکنش state/event/outbox | `engine.py`                               | `test_persistence.py` و ۱۳ سناریوی مشترک engine               |
| HTTP قابل‌لغو، retry، cooldown، ادغام درخواست و حذف پس از پایان واقعی                | `tsetmc.py`، `market_service.py`          | `test_market_client.py`، `test_market_parity.py`              |
| یک حلقهٔ پایش با هم‌زمانی محدود، فهرست مجاز پویا و union خصوصی/کانال                 | `monitor.py`                              | `test_monitor.py`، `test_market_parity.py`                    |
| پاک‌سازی بیرون نشست، دسته‌های هزارردیفی و join در توقف                               | `cleanup.py`                              | `test_persistence.py`، `test_monitor.py`، `test_runtime.py`   |
| claim فوری، اجارهٔ ۶۰ثانیه‌ای، شرط token و بازیابی اجارهٔ رهاشده                     | `delivery.py`                             | `test_delivery.py`                                            |
| deadline مشترک ده‌ثانیه‌ای، retry محدود و cooldown پایدار Telegram                   | `delivery.py`                             | `test_delivery.py` و ۱۶ سناریوی مشترک delivery                |
| بازبینی سن، revision، مجوز، watchlist، ترجیح و مقصد پیش از ارسال                     | `delivery.py`، `users.py`، `channels.py`  | `test_delivery.py`، شامل تغییر حین await                      |
| رویدادهای MTProto، ترتیب مصرف، خطای دائمی/موقت و وضعیت تجمیعی خصوصی                  | `bot.py`، `admin.py`                      | `test_bot.py`، `test_runtime.py`                              |
| startup شکست‌خورده، SIGINT/SIGTERM، لغو، join همهٔ workerها و آزاد شدن قفل           | `runtime.py`، `__main__.py`               | `test_runtime.py`، SIGTERM واقعی در `container_smoke.py`      |
| heartbeat اتمی، زنده بودن فرایند، پیشرفت مستقل، نوشتن DB و پاسخ اخیر MTProto         | `health.py`                               | `test_runtime.py`، `container_smoke.py`                       |
| لاگ ساختاریافته بدون token، محیط، update یا خطای خام                                 | `logger.py` و مسیرهای سرویس               | `test_runtime.py`، `test_bot.py`                              |
| بکاپ آنلاین WAL، integrity/FK، خروجی DELETE و نام یکتا                               | `backup.py`                               | `test_backup.py`، `container_smoke.py`                        |
| بازیابی به مسیر جدید و pruning فقط با تأیید، حفظ پیوند و فایل نامرتبط                | `backup.py`، `maintenance.py`             | `test_backup.py`                                              |
| helper محلی، تأیید صریح، پروژهٔ ثابت، بکاپ پیش از تغییر و تک‌نمونه                   | `operations.py`                           | `test_operations.py` با subprocess کاملاً ساختگی              |
| تصویر غیرریشه، volume داده، بدون پورت و حفظ تنظیمات Compose اصلی                     | `Dockerfile`، `compose.yaml`              | ساخت تصویر و `tests/smoke_image.py` با شبکهٔ قطع‌شده          |

مجموعهٔ پایه شامل ۱۴۵۲ ورودی مشترک ارزیاب، detector، تعریف، ترکیب، تنظیمات و نشست است. جریان‌های service، engine، market، bot و delivery نیز جداگانه با ماژول‌های آفلاین TypeScript مقایسه می‌شوند. کد شروع TypeScript در تست‌های تطبیقی import نمی‌شود.

تفاوت‌های عمدی ثبت‌شده حفظ شده‌اند: خطاهای تنظیمات فقط نام فیلد را گزارش می‌کنند؛ force-active فقط در محیط test پذیرفته می‌شود؛ کنترل مسیر تصویر symlink را resolve می‌کند؛ مالک NULL حتی برای پذیرش کانال قابل مشاهده نیست؛ توکن هر گام خصوصی تازه است؛ تاریخ نمایشی با برچسب صریح میلادی به وقت تهران نوشته می‌شود. متن پیام‌ها و لاگ‌ها تطبیق بایتی با TypeScript ندارند. حالت تجاری، اعداد، مسیر مجاز و مقصد ارسال در جریان‌های مشترک مقایسه می‌شوند.

در تحویل کانال، پس از تغییر دسترسی/کانال حین بررسی ناموفق مجوز نیز eligibility دوباره خوانده می‌شود و پیام بی‌مجوز لغو می‌شود. وضعیت EXPIRED/CANCELLED پس از await نهایی می‌ماند؛ به DELIVERED بازنویسی نمی‌شود. لغو انتظار یک caller تحویل را جایگزین یا تکثیر نمی‌کند. خاموش کردن برنامه، خود task تحویل را لغو و join می‌کند.

تحویل حداقل یک‌باره است؛ پذیرفته شدن پیام توسط Telegram و ثبت نشدن موفقیت می‌تواند پس از بازیابی تکرار ایجاد کند. این محدودیت با اجاره، کلید یکتای رخداد یا تست آفلاین حذف نمی‌شود.

نتیجهٔ عددی آخرین اجرای مجموعه‌ها، ساخت تصویر و خطاهای اصلاح‌شده در [سابقهٔ مهاجرت](MIGRATION.fa.md) ثبت می‌شود. دستورهای نصب، تست، ساخت و محدودیت‌های عملیاتی در [راهنمای پایتون](DEPLOYMENT.fa.md) آمده‌اند. نسخهٔ TypeScript، migrationهای اصلی و محیط واقعی حفظ شده‌اند.

## تغییر اتصال تلگرام به Telethon

به درخواست کاربر، نسخهٔ پایتون از `Telethon==1.45.0` و MTProto بات استفاده می‌کند؛ نسخهٔ TypeScript با grammY و long polling حفظ شده است. تنظیمات پایتون اکنون API ID و API hash معتبر نیز لازم دارد. نام‌های Bot API در facade داخلی به درخواست‌ها و رویدادهای MTProto تبدیل می‌شوند؛ هیچ HTTP Bot API در مسیر تولید پایتون باقی نمانده است.

`tests/test_telethon.py` با client ساختگی و انواع واقعی TL، رد گروه و کاربر غیرمجاز پیش از sender lookup، تطبیق فرستنده، پیام و callback، ورود فقط با bot token، مهاجرت محدود DC، دکمهٔ واقعی، peers عددی، مجوز دقیق کانال، FloodWait و خطاهای دائمی/موقت، deadline، لغو و disconnect را بررسی می‌کند. ساخت client واقعی فقط بدون اتصال شبکه و برای کنترل تنظیمات و ساخت markup انجام می‌شود. تست‌های تجاری قبلی از simulator داخل tests استفاده می‌کنند؛ این simulator در تصویر تولید کپی نمی‌شود.

سلامت به پاسخ واقعی `updates.GetState` نیاز دارد؛ صف خالی یا تایمر پیشرفت اتصال نیست. شکست probe رویدادهای در انتظار را حذف نمی‌کند. صف هزاررویدادی در صورت پرشدن، برنامه را با خطای صریح متوقف می‌کند. نشست حافظه‌ای است و بازپخش دستورهای زمان خاموشی تضمین نمی‌شود؛ outbox و تنظیمات پایدار SQLite حفظ می‌شوند. قرارداد بازار، منطق هشدار، مالکیت و بازبینی شرایط تحویل تغییری نکرده‌اند.

## پذیرش قابلیت گروه تلگرام

گروه معمولی و سوپرگروه با همان مقصد مشترک و بدون migration جدید پشتیبانی می‌شوند. ۹ تست تازه در `test_bot.py`، `test_config.py`، `test_delivery.py` و `test_telethon.py` تنظیم شناسه، مجوز خود بات، مقصد ثابت، مدیریت داخل گروه، جداسازی پیش‌نویس‌ها و اطلاعات خصوصی، عدم ثبت اعضا و تحویل outbox را بررسی می‌کنند. کل مجموعهٔ ۱۷۳تستی روی Windows و کانتینر Linux با شبکهٔ قطع‌شده موفق بود. متن ورودی فقط با Reply به آخرین پیام مرحله پذیرفته می‌شود؛ پیش‌نویس مدیر دیگر با دکمهٔ مشترک تغییر نمی‌کند. راهنمای گروه در [DEPLOYMENT.fa.md](DEPLOYMENT.fa.md) است.

## پروکسی MTProto و نصب اولیه

متغیر اختیاری `TELEGRAM_MTPROXY_URL` لینک کامل پروکسی را می‌پذیرد. انتقال داخلی Telethon برای secretهای معمولی و `dd` استفاده می‌شود؛ برای `ee`، احراز پاسخ Fake‑TLS و قاب‌بندی رکوردها به همان انتقال رمزگذاری‌شده اضافه شده است. وابستگی تازه‌ای اضافه نشده و در شکست پروکسی اتصال مستقیم جایگزین نمی‌شود. بازار از پروکسی تلگرام استفاده نمی‌کند. لینک واقعی کاربر در مخزن ذخیره نشده است.

پنج آزمون آفلاین در `tests/test_mtproxy.py` اعتبارسنجی و انتخاب انتقال، پنهان‌سازی secret، پاسخ‌های ساختگی با طول متغیر، رمزگذاری رفت‌وبرگشت Telethon، قاب‌بندی، خطای احراز، قطع ناقص، timeout و لغو اتصال را بررسی می‌کنند. مجموعهٔ ۱۷۸تستی روی Windows و Linux با شبکهٔ قطع‌شده، بررسی نوع و قالب، ساخت Docker و smoke test موفق بودند. اتصال واقعی به پروکسی و VPS بررسی نشده است. راهنمای استقرار برای اولین نصب بازنویسی شده است.
