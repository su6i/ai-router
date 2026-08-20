# Legacy AI Router Archive

این دایرکتوری شامل کدهای قدیمی مهارت `ai-router` است که از `agent-constitution` استخراج شده‌اند تا دانش و الگوهای منحصر‌به‌فرد از بین نروند. تمامی فایل‌های پایتون داخل فولدر `ai-router-skill-v0` کپی برابر اصل بوده و فقط جهت ارجاع هستند.

## جدول قابلیت‌های منحصربه‌فرد (UNIQUE)

| قابلیت | فایل:خط در آرشیو | وضعیت | چرا |
|---|---|---|---|
| 1. Anthropic Message Batches API | batch_queue.py:L177-L269 | archived-only | Batches APIِ Anthropic برای ما بی‌مصرف است (کانال premium اشتراک Claude Code است نه API پولی) |
| 2. OpenAI-Compatible FastAPI Proxy | server.py:L117-L322 | archived-only | پروکسیِ FastAPI را MCP جایگزین کرده |
| 3. HTTP Header & Body Routing Hints | server.py:L242-L265 | archived-only | پروکسیِ FastAPI را MCP جایگزین کرده |
| 4. Dynamic Config Module Loader | server.py:L93-L115 | archived-only | پروکسیِ FastAPI را MCP جایگزین کرده |
| 5. Interactive Role Config Wizard | configure.py:L1-L191 | archived-only | ویزاردِ `roles.yaml` معماریِ فعلی را ندارد |
| 6. UTC Peak-Window Surcharge | ai_router.py:L680-L716, batch_queue.py:L155-L168 | ported | هشدار در delegate.py نوشته شد |
| 7. Claude Effort Auto-Mapping | ai_router.py:L833-L842 | archived-only | effort الان در خودِ `MODELS`ِ `src/delegate.py` هست |
| 8. Prompt Length Max Tokens Heuristic | ai_router.py:L851-L856 | archived-only | effort الان در خودِ `MODELS`ِ `src/delegate.py` هست |
| 9. Anthropic Beta Context Editing | ai_router.py:L447-L448 | archived-only | effort الان در خودِ `MODELS`ِ `src/delegate.py` هست |
| 10. Interactive REPL Mode | router_cli.py:L89-L159 | archived-only | ویزاردِ `roles.yaml` معماریِ فعلی را ندارد |
| 11. Concurrent JSON Batch File Runner | router_cli.py:L176-L215 | archived-only | ویزاردِ `roles.yaml` معماریِ فعلی را ندارد |
| 12. Keyword-Based Complexity Analyzer | ai_router.py:L165-L240, README.md:L264-L303 | archived-only | ویزاردِ `roles.yaml` معماریِ فعلی را ندارد |

