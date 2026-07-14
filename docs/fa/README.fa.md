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
| `mcp/server.py` | سرور MCP-lite — `delegate_research`/`delegate_worker` را به‌عنوان ابزارِ MCP روی stdio می‌گشاید، پس هر host ای که MCP می‌فهمد بدون CLI هم delegationِ ارزان را کشف می‌کند |
| `tests/` | مجموعه‌ی تست pytest برای `src/delegate.py` و `mcp/server.py` |
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

فراخوانی‌های one-shot یکسان (همان model + system + prompt + max_output_tokens) خودکار به کشِ
exact-hash می‌خورند — تکرار هزینه‌ی $۰ دارد و اصلاً به provider نمی‌رسد. متون پیش از هش شدن با NFC نرمال‌سازی می‌شوند.

کش نهایتاً ۵۰۰۰ ردیف با عمر ۹۰ روز را نگه می‌دارد و به صورت بی‌صدا هنگام ثبت، موارد قدیمی را پاک‌سازی می‌کند. برای پاک‌سازی دستی:
```bash
python3 src/delegate.py --cache-prune
```

فراخوانی‌های `--session` هرگز کش نمی‌شوند (یک مکالمه‌ی چندمرحله‌ای از روی
یک turnِ کش‌شده‌ی تنها امن نیست). فراخوانیِ زنده را با `--no-cache` اجبار کنید:

```bash
python3 src/delegate.py --model flash -p "same prompt as before"            # cache HIT، $۰
python3 src/delegate.py --model flash -p "same prompt as before" --no-cache  # فراخوانیِ واقعی را اجبار می‌کند
```

### کش پرامپتِ Providerها (Provider prompt caches)

بسیاری از ارائه‌دهندگان API (مانند DeepSeek، Gemini، و MiniMax) پرامپت‌ها را بر اساس تطابق دقیق پیشوند (prefix) به‌طور خودکار کش می‌کنند. `delegate.py` این تخفیف‌ها را به‌طور خودکار محاسبه می‌کند:

- صرفه‌جویی مالی به‌طور واضح در هزینه‌ی چاپ‌شده منعکس می‌شود.
- نرخ موفقیتِ کشِ provider (مثلاً `cache hit rate: 85.0%`) در خلاصه‌ی worker و گزارش‌های `cost` نمایش داده می‌شود.
- حالت Worker از انضباط پیشوند (فایل‌ها اول، کار در آخر) برای به حداکثر رساندن بهره‌وری از کش پیشوند استفاده می‌کند.

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

### بودجه‌ها (Budgets)

سقف‌های بودجه با صدای بلند شکست می‌خورند — اگر یک پردازش از سقف خود عبور کند متوقف می‌شود؛ هزینه‌ی بیش از حدِ بی‌سروصدا ممنوع است. محدودیت‌ها در `<vault>/data/budgets.json` پیکربندی می‌شوند. اگر فایلی وجود نداشته باشد، هزینه بدون سقف است اما هشداری در stderr چاپ می‌شود.

اسکیما (به `budgets.example.json` در ریشه‌ی ریپو مراجعه کنید):

```json
{
  "monthly_usd": 5.0,
  "weekly_usd": 2.0,
  "per_session_usd": 0.50,
  "per_project_monthly_usd": {}
}
```

از فلگ `--estimate` برای اجرای آزمایشی (dry-run) استفاده کنید: توکن‌های تخمینی، هزینه و استفاده‌ی فعلی از بودجه را چاپ می‌کند بدون اینکه واقعا provider را فراخوانی کند یا در فایل audit بنویسد.

### گزارش هزینه

```bash
python3 src/delegate.py --cost --by model
```

فایل `audit.log` را در یک جدول متنی تراز شده از هزینه‌ها و نرخ موفقیتِ کش تجمیع می‌کند.

- `--cost` — مجموع کل زمان‌ها
- `--since YYYY-MM-DD` یا `--today` — فیلتر زمانی
- `--by <field>` — گروه‌بندی بر اساس `model` (پیش‌فرض)، `project`، `session`، `via` یا `day`

### Wrapper شل: `r()`

یک بار `shell/r.sh` را از rc شل خودت (bash یا zsh) بارگذاری کن:

```bash
echo 'source /Users/su6i/@-github/ai-router/shell/r.sh' >> ~/.zshrc
```

بعد از هر دایرکتوری، بدون واردشدن به context هیچ ایجنتی، کار را delegate کن:

```bash
r flash "write a regex that matches ISO-8601 dates"   # chat (words → one -p)
r gemini --files src/calc.py --allow-write "src/**" --verify "pytest -q" -p "fix the bug"
r cost --today                                        # print today's cost report
r audit                                               # print the ledger
```

آرگومان اول همیشه مدل است (نام ناشناخته با لیست aliasها خطای بلند می‌گیرد).
اگر آرگومان دوم با `-` شروع شود، همه‌چیز بدون تغییر به `delegate.py` پاس
می‌شود، پس همه‌ی فلگ‌ها کار می‌کنند. Override ها: `AI_ROUTER_REPO`،
`AI_ROUTER_PYTHON`.

### سرور MCP

`mcp/server.py` همان `delegate.py` (همان دفترکل، کش، سقف، مسیرِ secretها) را
به‌عنوان دو ابزارِ MCP می‌گشاید، پس هر MCP host ای — اول از همه Claude Code —
می‌تواند delegationِ ارزان را وسطِ کار کشف و استفاده کند، بدون این‌که کسی یادش
بماند بخواهد. یک‌بار با scope کاربر ثبت کن تا در همه‌ی پروژه‌ها در دسترس باشد:

```bash
claude mcp add --scope user ai-router -- python3 /Users/su6i/@-github/ai-router/mcp/server.py
```

فقط دو ابزار، هر دو سقف‌دار — هیچ‌وقت ابزار چتِ بدون سقف:

- **`delegate_research`** — جست‌وجوی واقعیت / چکِ داده‌ی زنده / راستی‌آزمایی
  سند (مدلِ پیش‌فرض `grok` = جست‌وجوی زنده‌ی وب/X). پاسخ با `max_output_tokens`
  سقف می‌خورد (پیش‌فرض ۵۰۰، سقف ۲۰۰۰) — یک پیش‌فرضِ پایین، نه یک قول.
- **`delegate_worker`** — خرکاریِ کدنویسی (مدلِ پیش‌فرض `gemini`). همان قرارداد
  worker mode در CLI: `files`/`allow_write`/`verify`/`retries` معادلِ
  `--files`/`--allow-write`/`--verify`/`--retries` هستند؛ `workdir` (مسیرِ
  مطلق) اجباری است چون فرآیندِ سرورِ MCP، cwd فراخواننده را ارث نمی‌برد. فقط
  همان خلاصه‌ی ≤۲۵ خطیِ قبلی برمی‌گردد — کدِ تولیدی هرگز از سیم رد نمی‌شود.

مدل‌های Claude همچنان داخل delegate ممنوع‌اند (بدونِ تغییر). ردیف‌های audit از
فراخوانی‌های MCP یک فیلدِ `via: "mcp"` می‌گیرند (فیلدِ اضافه‌ای کنارِ ستون‌های
قبلی) تا هزینه-به-ازای-هر-در یک کوئری باشد؛ ردیف‌های `r()`/CLI همان‌طور
می‌مانند (فیلد غایب است، نه null). Transport: فقط stdio، فقط ماشینِ محلی، بدون
HTTP/SSE، بدون auth (خارج از scope نسخه‌ی ۱).

### ماشه‌های واگذاری (تا معمار واقعاً ابزارها را صدا بزند)

ابزاری که صرفاً «وجود دارد» صدا زده نمی‌شود — مدلِ معمارِ گران به‌طور پیش‌فرض
خودش کد می‌نویسد. دو لایه او را به سمت worker هُل می‌دهند:

- **توضیحاتِ دستوریِ ابزارها** — هر دو description می‌گویند این ابزار را
  *به‌جای* Edit/Write یا WebSearch کِی باید صدا زد (پیاده‌سازیِ بیش از ~۴۰
  خط، فایل‌های تست، تغییرِ مکانیکی در چند فایل؛ واقعیتِ زنده / راستی‌آزمایی
  سند)، به‌علاوه‌ی قانون طلایی: تصمیمِ واگذاری **قبل از** خواندنِ فایل‌های
  هدف — مسیر بده، نه محتوا.
- **`hooks/delegate_nudge.py`** — یک هوکِ PreToolUse (ثبتِ سراسری در
  `~/.claude/settings.json`، با matcher `Write|Edit`) که *اولین* نوشتنِ
  بزرگِ کد را (بیش از ۴۰ خطِ جدید، فقط پسوندهای کد؛ docs/config/scratchpad
  معاف) با یادآوریِ `delegate_worker` رد می‌کند. تلاشِ دوم روی همان فایل در
  همان session عبور می‌کند — دریچه‌ی فرارِ عمدی برای کدِ معماری-بحرانی.
  Fail-open: هر خطای هوک یعنی اجازه‌ی نوشتن.

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

### مقاومت و Fallbackها (Resilience)

- **تلاشِ مجدد (Retries)**: تمامیِ درخواست‌ها در صورت بروز خطاهای موقت (مانند HTTP 429، 5xx، یا timeout) با تأخیر تصاعدی (۱ ثانیه و سپس ۳ ثانیه) تکرار می‌شوند. خطاهای قطعی (مانند HTTP 400 یا پاسخِ نامعتبر JSON) بلافاصله متوقف شده و خطای صریح `ProviderError` می‌دهند.
- **بازگشتِ MiniMax**: اگر مدلِ پیش‌پرداختِ `minimax` به دلیل اتمام اعتبار یا خطاهای 401/402/429 پس از تلاش‌های مجدد شکست بخورد، روتر به‌طور خودکار به مدلِ `flash` (`deepseek-v4-flash`) سوییچ می‌کند.
- **بازگشتِ Gemini**: اگر مدلِ رایگانِ `gemini` به دلیل محدودیت‌های نرخِ درخواست (HTTP 429) پس از تلاش‌های مجدد مسدود شود، روتر به‌طور خودکار به مدلِ `flash` سوییچ می‌کند. از آنجایی که این بازگشت دیگر رایگان نیست، هشداری صریح چاپ می‌شود.

## وضعیت

اسکلت زیرساخت — اسکیما و سرویس‌ها مرحله‌به‌مرحله ساخته می‌شوند.
برنامه‌ی فازبندی‌شده در `docs/ARCHITECTURE.md`.

## راه‌اندازی (Data Plane)

1. شروع Postgres:
   ```bash
   cp .env.example .env
   docker compose up -d
   ```
   نیازمند Docker (روی macOS با Colima تست شده). اسکیمای `usage` در اولین اجرا به‌طور خودکار اعمال می‌شود.

2. اتصال به دیتابیس را در فایل secrets والت خود (`~/.local/share/agent-projects/ai-router/secrets/.env`) تنظیم کنید:
   ```ini
   POSTGRES_DSN=postgresql://airouter:change-me@localhost:5432/airouter
   ```

3. لاگ‌های audit موجود را وارد Postgres کنید:
   ```bash
   uv run src/ingest.py
   ```

## تست

```bash
cd /Users/su6i/@-github/ai-router
uv sync --group dev
uv run pytest -q
```

نتیجه‌ی انتظاری: `73 passed` (همه‌ی suiteها زیر `tests/` — کاملاً آفلاین،
بدون نیاز به کلید API یا vault).
