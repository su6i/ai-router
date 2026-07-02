# ai-router

[English](README.md) · **[معماری](docs/ARCHITECTURE.md)**

دروازه‌ی LLM با حسابداری هزینه: یک درِ واحد به همه‌ی مدل‌ها؛ هر فراخوانی
برچسب‌خورده، بودجه‌دار و ثبت‌شده در دفترکل. زیرساخت همراه برای پروژه‌های
چنداجنتی که می‌خواهند **هزینه‌ی هر task یک کوئری SQL باشد**، نه یک حدس.
طراحی کامل: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## چه چیزی این‌جاست

| مسیر | توضیح |
| --- | --- |
| `src/delegate.py` | درگاه واحد LLM برای خرکاری — اثباتِ echo‌شده از سمت provider، کشِ exact-hash، حافظه‌ی session، worker mode (`--files`)، دفترِ audit |
| `tests/` | مجموعه‌ی تست pytest برای `src/delegate.py` |
| `docs/ARCHITECTURE.md` | طراحی کامل: اسکیمای Postgres + pgvector، کش exact-hash پرامپت، مانیتورینگ Prometheus/Grafana |
| `docker-compose.yml` | Postgres (pgvector) + پشته‌ی مانیتورینگ |
| `.env.example` | متغیرهای محیطی لازم (کپی کنید، پر کنید، هرگز commit نکنید) |
| `CHANGELOG.md` | تغییرات قابل‌توجه، جدیدترین در بالا |

`delegate.py` هیچ state ای داخل ریپو نگه نمی‌دارد: کش، audit log و حافظه‌ی
session در والت هستند (`~/.local/share/agent-projects/ai-router/data/`،
قابل override با `AI_ROUTER_DATA_DIR`)؛ secretها از `<vault>/secrets/.env`
روی `_shared/secrets/.env` لایه می‌شوند.

## استفاده

### چتِ یک‌باره (one-shot)

```bash
python3 src/delegate.py --model flash -p "summarize this changelog"
```

`--model` یک alias می‌پذیرد (پیش‌فرض `minimax`؛ یا `flash`، `pro`، `grok`،
`gemini`، `gemini-lite`، `gemma`، یا نام کامل مدل — لیستِ کامل در `ALIASES`
داخل `src/delegate.py`). `--plan <file>` پرامپت را از یک فایل می‌خواند
(به‌جای `-p`)؛ `--out <file>` پاسخ را در یک فایل می‌نویسد (به‌جای stdout).

### Session

```bash
python3 src/delegate.py --model flash --session refactor-foo \
  -p "list the functions in src/foo.py that need docstrings"
python3 src/delegate.py --model flash --session refactor-foo \
  -p "now write docstrings for the ones you listed"
```

`--session <name>` مکالمه را بین فراخوانی‌ها، با کلیدِ همان نام، در والت
نگه می‌دارد (هرگز در ریپو). `--new` سشنِ نام‌گذاری‌شده را قبل از اجرا ریست
می‌کند. `--system <text>` یک persona/دستور سیستم تنظیم می‌کند.

### کش

فراخوانی‌های one-shot یکسان (همان model + system + prompt) خودکار به کشِ
exact-hash می‌خورند — تکرار هزینه‌ی $۰ دارد و اصلاً به provider نمی‌رسد.
فراخوانی‌های `--session` هرگز کش نمی‌شوند (یک مکالمه‌ی چندمرحله‌ای از روی
یک turnِ کش‌شده‌ی تنها امن نیست). فراخوانیِ زنده را با `--no-cache` اجبار کنید:

```bash
python3 src/delegate.py --model flash -p "same prompt as before"            # cache HIT، $۰
python3 src/delegate.py --model flash -p "same prompt as before" --no-cache  # فراخوانیِ واقعی را اجبار می‌کند
```

### Worker mode

`delegate.py --files` به یک مدل ارزان دسترسی مستقیم خواندن/نوشتن روی فایل‌های
دیسک می‌دهد — به‌جای برگرداندن کد به‌عنوان متن چت، کد تولیدی هرگز وارد context
فراخواننده نمی‌شود، فقط یک خلاصه‌ی کوتاه:

```bash
python3 src/delegate.py --model flash \
  --files "src/foo.py,tests/test_foo.py" \
  --allow-write "src/**,tests/**" \
  --verify "uv run pytest -q" \
  --retries 1 \
  -p "add a docstring to foo()"
```

- `--files` — لیستِ فایل‌هایی (جدا با کاما) که worker می‌خواند و ممکن است بازنویسی کند.
- `--allow-write` — globِ (نسبت به cwd، جدا با کاما) هر نوشتنی را دروازه می‌کند؛ بدون فلگ یعنی بدون نوشتن.
- `--verify` — دستور شلی که فراخواننده مشخص می‌کند (هرگز حدس زده نمی‌شود).
- `--retries` — تعداد تلاشِ مجدد در صورتِ شکستِ verify (پیش‌فرض ۱، سقف ۲)؛ worker خروجیِ verify را پس می‌گیرد و یک تلاشِ دیگر به‌ازای هر retry دارد.

خروجی — تنها چیزی که به contextِ فراخواننده می‌رسد:

```
files written : src/foo.py (312B)
rejected      : (none)
verify        : uv run pytest -q → PASS (1.2s)   [attempt 1/2]
worker summary: added a one-line docstring to foo()
cost          : $0.000421 · model echoed: deepseek-v4-flash
```

پروتکل کامل: سند طراحی خصوصی `DELEGATE-TOOL-DESIGN.md` (والت).

### Audit

```bash
python3 src/delegate.py --audit
```

`audit.log` را چاپ می‌کند (یک خط JSON به‌ازای هر فراخوانی: مدلِ درخواستی/echo‌شده،
session، project، commit، هزینه، cached؛ فراخوانی‌های worker mode هم
files written/rejected، دستور/وضعیتِ verify، تعداد attempt را اضافه می‌کنند).

## مدل‌ها

از `MODELS` در `src/delegate.py` (هزینه به ازای هر ۱ میلیون توکن):

| `--model` | مدلِ API | Provider | هزینه‌ی in / out | نقش |
| --- | --- | --- | --- | --- |
| `minimax` | `MiniMax-M3` | MiniMax | $0.30 / $1.20 | پیش‌فرض — اعتبارِ یک‌بارمصرفِ پرداخت‌شده، اول همین خرج شود |
| `flash` | `deepseek-v4-flash` | DeepSeek | $0.14 / $0.28 | خرکاریِ عمومی — پیاده‌سازی، refactor، تست، boilerplate |
| `pro` | `deepseek-v4-pro` | DeepSeek | $0.435 / $0.87 | Reasoner — هدفِ escalation وقتی `flash` شکست بخورد یا استدلالِ عمیق‌تر لازم باشد |
| `grok` | `grok-4.3` | xAI | $1.25 / $2.50 | نظرِ دوم / دانشِ روز — نه برای کارِ روتین |
| `gemini` | `gemini-2.5-flash` | Google (رده‌ی رایگان) | $0 / $0 | خرکاریِ رایگان — پیام‌های commit، تبدیلِ فرمت، دسته‌بندی |
| `gemini-lite` | `gemini-2.5-flash-lite` | Google (رده‌ی رایگان) | $0 / $0 | رده‌ی رایگان، نسخه‌ی سبک‌تر/سریع‌ترِ `gemini` |
| `gemma` | `gemma-4-31b-it` | Google (رده‌ی رایگان) | $0 / $0 | رده‌ی رایگان، مدلِ open-weight |

ترتیبِ اولویت و منطقِ کاملِ روتینگ (fallbackِ اتمامِ اعتبارِ MiniMax، چرا Claude
هرگز در این روتر نیست، تفاوتِ provider در برابرِ subscription-CLI):
`STRATEGY.md` و `ROLES.md` در `~/.local/share/agent-projects/_router/`
(والت، نه در این ریپو).

## وضعیت

اسکلت زیرساخت — اسکیما و سرویس‌ها مرحله‌به‌مرحله ساخته می‌شوند.
برنامه‌ی فازبندی‌شده در `docs/ARCHITECTURE.md`.

## راه‌اندازی

```bash
cp .env.example .env
docker compose up -d
```

نیازمند Docker (روی macOS با Colima تست شده).

## تست

```bash
cd /Users/su6i/@-github/ai-router
uv run --with pytest --with httpx pytest
```

نتیجه‌ی انتظاری: `28 passed` (`tests/test_delegate_cache.py` +
`tests/test_delegate_worker.py`).
