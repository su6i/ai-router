# ai-router

[English](README.md)

دروازه‌ی LLM با حسابداری هزینه: یک درِ واحد به همه‌ی مدل‌ها؛ هر فراخوانی
برچسب‌خورده، بودجه‌دار و ثبت‌شده در دفترکل. زیرساخت همراه برای پروژه‌های
چنداجنتی که می‌خواهند **هزینه‌ی هر task یک کوئری SQL باشد**، نه یک حدس.

## چه چیزی این‌جاست

| مسیر | توضیح |
| --- | --- |
| `docs/ARCHITECTURE.md` | طراحی کامل: اسکیمای Postgres + pgvector، کش exact-hash پرامپت، مانیتورینگ Prometheus/Grafana |
| `docker-compose.yml` | Postgres (pgvector) + پشته‌ی مانیتورینگ |
| `.env.example` | متغیرهای محیطی لازم (کپی کنید، پر کنید، هرگز commit نکنید) |

## وضعیت

اسکلت زیرساخت — اسکیما و سرویس‌ها مرحله‌به‌مرحله ساخته می‌شوند.
برنامه‌ی فازبندی‌شده در `docs/ARCHITECTURE.md`.

## راه‌اندازی

```bash
cp .env.example .env
docker compose up -d
```

نیازمند Docker (روی macOS با Colima تست شده).
