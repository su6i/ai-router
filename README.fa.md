# ai-router

[English](README.md)

دروازه‌ی LLM با حسابداری هزینه: یک درِ واحد به همه‌ی مدل‌ها؛ هر فراخوانی
برچسب‌خورده، بودجه‌دار و ثبت‌شده در دفترکل. زیرساخت همراه برای پروژه‌های
چنداجنتی که می‌خواهند **هزینه‌ی هر task یک کوئری SQL باشد**، نه یک حدس.

## چه چیزی این‌جاست

| مسیر | توضیح |
| --- | --- |
| `src/delegate.py` | درگاه واحد LLM برای خرکاری — اثباتِ echo‌شده از سمت provider، کشِ exact-hash، حافظه‌ی session، دفترِ audit |
| `tests/` | مجموعه‌ی تست pytest برای `src/delegate.py` |
| `docs/ARCHITECTURE.md` | طراحی کامل: اسکیمای Postgres + pgvector، کش exact-hash پرامپت، مانیتورینگ Prometheus/Grafana |
| `docker-compose.yml` | Postgres (pgvector) + پشته‌ی مانیتورینگ |
| `.env.example` | متغیرهای محیطی لازم (کپی کنید، پر کنید، هرگز commit نکنید) |

`delegate.py` هیچ state ای داخل ریپو نگه نمی‌دارد: کش، audit log و حافظه‌ی
session در والت هستند (`~/.local/share/agent-projects/ai-router/data/`،
قابل override با `AI_ROUTER_DATA_DIR`)؛ secretها از `<vault>/secrets/.env`
روی `_shared/secrets/.env` لایه می‌شوند. اجرای تست‌ها:

```bash
uv run --with pytest --with httpx pytest
```

## وضعیت

اسکلت زیرساخت — اسکیما و سرویس‌ها مرحله‌به‌مرحله ساخته می‌شوند.
برنامه‌ی فازبندی‌شده در `docs/ARCHITECTURE.md`.

## راه‌اندازی

```bash
cp .env.example .env
docker compose up -d
```

نیازمند Docker (روی macOS با Colima تست شده).
