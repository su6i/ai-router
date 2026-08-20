---
name: ai-router
description: Production-ready multi-model AI router — routes by task complexity to the cheapest capable model (DeepSeek/MiniMax/Claude tiers), with prompt-cache reads, response caching, fallback, circuit breaker, cost tracking, off-peak gating, effort mapping, and an OpenAI-compatible FastAPI proxy. Use when configuring cost-optimized LLM routing, a Cline/OpenAI-compatible proxy, or batch-deferred non-urgent jobs.
version: 1.5.2
updated: 2026-06-30
---

# AI Router - Professional Multi-Model Routing System

یک سیستم حرفه‌ای و production-ready برای routing هوشمند بین مدل‌های مختلف AI با تمرکز بر بهینه‌سازی هزینه.

## Files

| File | Purpose |
|---|---|
| `ai_router.py` | Core engine — `AIRouter`, `RoutingConfig`, model clients, complexity analyser |
| `server.py` | FastAPI OpenAI-compatible proxy (`POST /v1/chat/completions`) — point Cline here |
| `batch_queue.py` | Non-urgent job queue; flushes to Anthropic Batches API (50% off) or off-peak |
| `router_cli.py` | CLI for quick one-shot queries |
| `configure.py` | Interactive wizard: pick planning/acting model priorities, writes `roles.yaml`, prints Cline snippet |
| `config_example.py` | Annotated example configuration (copy to your vault, fill in keys) |
| `requirements.txt` | Python dependencies |

### Quick start (proxy)

> **`anthropic` is optional.** Only the Claude client needs it; the grunt-work
> providers (DeepSeek / MiniMax / Grok) use an OpenAI-compatible httpx client. A
> subscription-only setup with **no Anthropic API key** can run the router
> without installing `anthropic` — just omit Claude models from the config.

```bash
# Install deps
pip install -r requirements.txt

# Point at your personal config (exports build() -> AIRouter)
export AIROUTER_CONFIG_MODULE=config   # or vault.config, etc.

# (Optional) pick your model priorities interactively
python configure.py           # or: router configure

# Start the proxy
uvicorn server:app --host 0.0.0.0 --port 8787
```

Cline: set `base_url = http://localhost:8787/v1` in settings (Act model only — Plan model uses the subscription CLI provider, NOT the proxy).

> **Note on install.sh:** `install.sh` stays zero-touch. After it finishes it prints:
> `"Run \`router configure\` to choose your planning/acting model priorities."`

## ویژگی‌ها

### Core Features

- ✅ **Intelligent Routing**: انتخاب خودکار بهترین مدل بر اساس پیچیدگی task
- ✅ **Cost Optimization**: کاهش هزینه با استفاده بهینه از مدل‌های مختلف
- ✅ **Response Caching**: کش کردن پاسخ‌ها برای کاهش هزینه و latency
- ✅ **Automatic Fallback**: در صورت خطا به مدل دیگری سوئیچ می‌کنه
- ✅ **Circuit Breaker**: جلوگیری از درخواست‌های مکرر به سرویس‌های down
- ✅ **Cost Tracking**: ردیابی دقیق هزینه‌ها و تخمین هزینه ماهانه
- ✅ **Concurrent Requests**: پشتیبانی از درخواست‌های همزمان با rate limiting
- ✅ **Async/Await**: معماری async برای performance بالا

### Advanced Features

- Complexity analysis با ML-based scoring
- Token usage optimization
- Health checks و monitoring
- Detailed logging و metrics
- Configurable routing strategies
- Multi-model fallback chains

## Supported Providers

| ModelType | Client | base_url |
|---|---|---|
| `CLAUDE_OPUS / SONNET / HAIKU` | `ClaudeClient` (Anthropic SDK) | — |
| `DEEPSEEK_FLASH / PRO` | `DeepSeekClient` (httpx) | `https://api.deepseek.com/v1` |
| `GROK` | `OpenAICompatibleClient` | `https://api.x.ai/v1` — VERIFY model ID at docs.x.ai |
| `OPENAI` | `OpenAICompatibleClient` | `https://api.openai.com/v1` — VERIFY model ID at platform.openai.com |
| `MINIMAX` | `OpenAICompatibleClient` | `https://api.minimax.io/v1` |

`OpenAICompatibleClient` handles any provider that exposes a standard
`POST /chat/completions` endpoint (OpenAI request shape). Adding a new
provider requires only a new `ModelType` enum member and a `ModelConfig` entry —
no new client class needed.

## Plan/Act Role Routing

Pass `role="planning"` or `role="acting"` (or any custom key) in `generate()` kwargs.
If the role is present in `RoutingConfig.roles`, its ordered model list is tried first —
circuit-open models are skipped, complexity routing is the final fallback.

```python
routing = RoutingConfig(
    roles={
        # set personal choices in your vault config, not here
        "planning": (ModelType.CLAUDE_OPUS, ModelType.CLAUDE_SONNET),
        "acting":   (ModelType.DEEPSEEK_PRO, ModelType.MINIMAX, ModelType.GROK),
    }
)

response = await router.generate(prompt, role="acting")
```

`RoutingConfig.roles` defaults to `{}` (empty) — no personal model choices in the shared engine.

## 6 Cost Techniques (RoutingConfig fields)

All fields have **neutral defaults** (off/empty). Set them in your personal config.

| Field | Default | Purpose |
|---|---|---|
| `prefer_models` | `()` | Try these models first (e.g. to spend prepaid credit) |
| `complexity_to_effort` | `{}` | Map complexity int → `"low"/"medium"/"high"` effort for Claude extended-thinking |
| `enable_prompt_cache` | `True` | Mark stable system prompt with `cache_control` (Claude ~0.1× read price) |
| `batch_flush_seconds` | `0` | Flush non-urgent jobs to Anthropic Batches (50% off) every N seconds |
| `batch_max` | `100` | Flush immediately when queue reaches this size |
| `peak_windows_utc` | `[]` | List of `[start, end]` UTC hour pairs where pricing is higher |
| `peak_multiplier` | `1.0` | Cost multiplier applied during peak windows |
| `max_tokens_policy` | `"fixed"` | `"estimate"` = derive max_tokens from prompt length |
| `enable_context_editing` | `False` | Enable `clear_tool_uses_20250919` beta for long agentic sessions |

Example personal config (DeepSeek peak windows, MiniMax credit preference):

```python
routing = RoutingConfig(
    prefer_models=(ModelType.MINIMAX,),          # spend prepaid credit first
    complexity_to_effort={1: "low", 2: "low", 3: "medium", 4: "high"},
    peak_windows_utc=[[1, 4], [6, 10]],          # DeepSeek peak UTC hours
    peak_multiplier=2.0,                          # 2× during peak
    batch_flush_seconds=60,
    batch_max=50,
    max_tokens_policy="estimate",
    enable_context_editing=True,
)
```

## نصب و راه‌اندازی

### 1. نصب Dependencies

```bash
pip install -r requirements.txt
```

### 2. تنظیم API Keys

فایل `.env` بساز:

```bash
# Claude API
ANTHROPIC_API_KEY=your_claude_api_key_here

# DeepSeek API
DEEPSEEK_API_KEY=your_deepseek_api_key_here
```

### 3. Configuration

Router رو با مدل‌های موردنظرت configure کن:

```python
from ai_router import AIRouter, ModelConfig, ModelType, RoutingConfig

# تعریف مدل‌ها
model_configs = {
    ModelType.CLAUDE_OPUS: ModelConfig(
        name="claude-opus-4-6",
        api_key="<REDACTED>",
        input_cost_per_1m=5.0,
        output_cost_per_1m=25.0,
        max_tokens=8192
    ),
    ModelType.DEEPSEEK_CODER: ModelConfig(
        name="deepseek-coder",
        api_key="<REDACTED>",
        base_url="https://api.deepseek.com/v1",
        input_cost_per_1m=0.14,
        output_cost_per_1m=0.28,
        max_tokens=4096
    )
}

# تنظیمات routing
routing_config = RoutingConfig(
    deepseek_max_complexity=2,  # Tasks with complexity ≤2 → DeepSeek
    enable_caching=True,
    enable_fallback=True,
    max_concurrent_requests=10
)

router = AIRouter(model_configs, routing_config)
```

## استفاده

### مثال ساده

```python
import asyncio
from ai_router import AIRouter

async def main():
    router = AIRouter(model_configs, routing_config)
    
    # درخواست ساده - خودکار DeepSeek انتخاب میشه
    response = await router.generate(
        "Write a simple Python function to calculate fibonacci"
    )
    
    print(f"Model: {response['model']}")
    print(f"Cost: ${response['cost_usd']:.4f}")
    print(f"Response: {response['response']}")
    
    await router.cleanup()

asyncio.run(main())
```

### مثال پیشرفته با Context

```python
# Task پیچیده با context
response = await router.generate(
    prompt="Design a distributed caching system with Redis",
    context={
        'environment': 'production',
        'urgent': True,
        'file_count': 15
    }
)

# این خودکار Claude Opus رو انتخاب می‌کنه چون:
# - Context نشون میده production environment
# - urgent flag فعاله
# - تعداد فایل‌ها زیاده
```

### Force کردن مدل خاص

```python
response = await router.generate(
    "Explain design patterns",
    force_model=ModelType.CLAUDE_HAIKU
)
```

### دریافت آمار و هزینه‌ها

```python
# آمار کلی
stats = await router.get_stats()
print(stats)

# تخمین هزینه ماهانه
monthly_estimate = await router.cost_tracker.estimate_monthly_cost(
    requests_per_day=500
)
print(f"Estimated monthly: ${monthly_estimate['estimated_monthly_cost']}")
```

## استراتژی Routing

### سطوح پیچیدگی

Router بر اساس این معیارها complexity رو تشخیص میده:

**TRIVIAL (1)**: formatting, indentation, comments, imports
→ DeepSeek Coder

**SIMPLE (2)**: CRUD operations, basic validation, simple tests
→ DeepSeek Coder

**MODERATE (3)**: Business logic, API endpoints, database operations
→ Claude Haiku

**COMPLEX (4)**: Architecture decisions, optimization, async/distributed systems
→ Claude Sonnet

**CRITICAL (5)**: Production bugs, security issues, data loss prevention
→ Claude Opus

### Keywords برای تشخیص پیچیدگی

Router از این کلمات کلیدی استفاده می‌کنه:

```python
# TRIVIAL
['format', 'indent', 'comment', 'rename', 'boilerplate']

# SIMPLE
['crud', 'getter', 'setter', 'validate', 'parse']

# MODERATE
['implement', 'refactor', 'business logic', 'api endpoint']

# COMPLEX
['architecture', 'design pattern', 'algorithm', 'performance', 'scalability']

# CRITICAL
['bug fix production', 'security vulnerability', 'data loss', 'emergency']
```

## بهینه‌سازی هزینه

### مثال واقعی

فرض کن روزی 500 درخواست داری:

**بدون Router (همه Opus 4.6):**

- هزینه روزانه: ~$27.5
- هزینه ماهانه: ~$825

**با Router (70% DeepSeek + 30% Claude):**

- 350 درخواست DeepSeek: ~$0.70/day
- 150 درخواست Claude: ~$8.25/day
- **هزینه روزانه: ~$9**
- **هزینه ماهانه: ~$270**

**صرفه‌جویی: 67%** 💰

### نکات بهینه‌سازی

1. **Enable Caching**: درخواست‌های تکراری رو cache کن

```python
routing_config = RoutingConfig(
    enable_caching=True,
    cache_ttl_seconds=3600  # 1 hour
)
```

1. **Adjust Complexity Thresholds**: threshold ها رو تنظیم کن

```python
routing_config = RoutingConfig(
    deepseek_max_complexity=3,  # بیشتر از DeepSeek استفاده کن
    sonnet_max_complexity=5      # کمتر از Opus استفاده کن
)
```

1. **Monitor و Tune**: آمارها رو چک کن و تنظیم کن

```python
stats = await router.get_stats()
print(f"Cache hit rate: {stats['cost_summary']['cache_hit_rate']}")
```

## Circuit Breaker

وقتی یه مدل مشکل داره، circuit breaker باز میشه:

```python
# بعد از 5 خطای متوالی
circuit_breaker.state = 'open'

# بعد از 60 ثانیه، half-open میشه و یه بار امتحان می‌کنه
# اگه موفق بود → closed
# اگه fail کرد → دوباره open
```

## Monitoring و Logging

### Log Levels

```python
import logging

# تنظیم log level
logging.getLogger('AIRouter').setLevel(logging.DEBUG)
```

### Metrics

```python
# آمار 24 ساعت گذشته
stats_24h = await router.cost_tracker.get_summary(
    timedelta(hours=24)
)

print(f"Total cost (24h): ${stats_24h['total_cost']}")
print(f"Cache hit rate: {stats_24h['cache_hit_rate']}")
print(f"Error rate: {stats_24h['error_rate']}")
```

## Advanced Usage

### Custom Complexity Analyzer

می‌تونی analyzer خودت رو بنویسی:

```python
from ai_router import ComplexityAnalyzer, TaskComplexity

class CustomAnalyzer(ComplexityAnalyzer):
    @staticmethod
    def analyze(prompt, context=None):
        # لاجیک خودت
        if 'database migration' in prompt.lower():
            return TaskComplexity.CRITICAL, 1.0
        return TaskComplexity.SIMPLE, 0.5

# استفاده از custom analyzer
router.complexity_analyzer = CustomAnalyzer()
```

### Streaming Responses

برای پاسخ‌های بلند:

```python
# TODO: Implement streaming
# این feature رو می‌تونی خودت اضافه کنی
async def generate_stream(self, prompt, **kwargs):
    # Implementation here
    pass
```

## Testing

```bash
# اجرای مثال‌ها
python ai_router.py

# تست با prompts مختلف
python -c "
import asyncio
from ai_router import AIRouter

async def test():
    router = AIRouter(configs)
    
    # تست complexity detection
    prompts = [
        'format this code',
        'implement user authentication',
        'design microservices architecture'
    ]
    
    for prompt in prompts:
        response = await router.generate(prompt)
        print(f'{prompt[:30]}... → {response['model']}')

asyncio.run(test())
"
```

## Troubleshooting

### خطای "Circuit breaker open"

مدل مشکل داره. چند دقیقه صبر کن یا از `force_model` استفاده کن.

### خطای "All models failed"

همه مدل‌ها down هستن. API keys رو چک کن.

### هزینه زیاد

1. `enable_caching=True` رو فعال کن
2. `deepseek_max_complexity` رو افزایش بده
3. آمارها رو بررسی کن: `await router.get_stats()`

## Performance Tips

1. **Concurrent Requests**: `max_concurrent_requests` رو افزایش بده
2. **Cache TTL**: برای queries تکراری TTL رو زیاد کن
3. **Timeout**: timeout رو کم کن اگه latency مهمه
4. **Batch Processing**: چند prompt رو با `asyncio.gather` همزمان بفرست

```python
async def batch_generate(prompts):
    tasks = [router.generate(p) for p in prompts]
    return await asyncio.gather(*tasks)
```

## Roadmap

- [ ] Support برای مدل‌های بیشتر (GPT-4, Gemini, etc.)
- [ ] Streaming responses
- [ ] Load balancing بین چند instance از یک مدل
- [ ] Prometheus metrics export
- [ ] Web UI برای monitoring
- [ ] A/B testing بین مدل‌ها
- [ ] Fine-tuned routing با ML

## License

MIT License - استفاده آزاد در پروژه‌های شخصی و تجاری

## Support

سوال یا مشکلی داری؟

- Issue بزن توی GitHub
- یا مستقیم بپرس!

---

**ساخته شده با ❤️ برای برنامه‌نویسان حرفه‌ای**
