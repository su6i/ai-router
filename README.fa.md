# ai-router

[English](README.md)

دروازه‌ی LLM با حسابداری هزینه: یک درِ واحد به همه‌ی مدل‌ها؛ هر فراخوانی
برچسب‌خورده، بودجه‌دار و ثبت‌شده در دفترکل. زیرساخت همراه برای پروژه‌های
چنداجنتی که می‌خواهند **هزینه‌ی هر task یک کوئری SQL باشد**، نه یک حدس.

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
روی `_shared/secrets/.env` لایه می‌شوند. اجرای تست‌ها:

```bash
uv run --with pytest --with httpx pytest
```

### Worker mode

`delegate.py --files` به یک مدل ارزان دسترسی مستقیم خواندن/نوشتن روی فایل‌های
دیسک می‌دهد — به‌جای برگرداندن کد به‌عنوان متن چت، کد تولیدی هرگز وارد context
فراخواننده نمی‌شود، فقط یک خلاصه‌ی کوتاه (حداکثر ۲۵ خط):

```bash
python3 src/delegate.py --model flash \
  --files "src/foo.py,tests/test_foo.py" \
  --allow-write "src/**,tests/**" \
  --verify "uv run pytest -q" \
  -p "add a docstring to foo()"
```

`--allow-write` (globِ نسبت به cwd) هر نوشتنی را دروازه می‌کند — بدون فلگ یعنی
بدون نوشتن. `--verify` دستور شلی است که فراخواننده مشخص می‌کند (هرگز حدس زده
نمی‌شود). پروتکل کامل در سند طراحی خصوصی `DELEGATE-TOOL-DESIGN.md` (والت).

## وضعیت

اسکلت زیرساخت — اسکیما و سرویس‌ها مرحله‌به‌مرحله ساخته می‌شوند.
برنامه‌ی فازبندی‌شده در `docs/ARCHITECTURE.md`.

## راه‌اندازی

```bash
cp .env.example .env
docker compose up -d
```

نیازمند Docker (روی macOS با Colima تست شده).
