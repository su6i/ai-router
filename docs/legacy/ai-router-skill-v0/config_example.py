# FROZEN REFERENCE — not executed.
# Origin: agent-constitution skills/ai-router @ 9b2be20 (2026-07-01).
# See docs/legacy/README.md.
"""
Example configuration for AI Router
Copy to config.py and fill in your API keys
"""

from ai_router import ModelConfig, ModelType, RoutingConfig

# =============================================================================
# Model Configurations
# =============================================================================

MODEL_CONFIGS = {
    # Claude Opus 4.8 - Most powerful, most expensive
    ModelType.CLAUDE_OPUS: ModelConfig(
        name="claude-opus-4-8",
        api_key="<REDACTED>",  # Get from https://console.anthropic.com
        input_cost_per_1m=5.0,    # $5 per 1M input tokens
        output_cost_per_1m=25.0,  # $25 per 1M output tokens
        max_tokens=8192,
        timeout=120,
        max_retries=3,
        rate_limit_per_minute=50
    ),
    
    # Claude Sonnet 4.6 - Balanced performance/cost
    ModelType.CLAUDE_SONNET: ModelConfig(
        name="claude-sonnet-4-6",
        api_key="<REDACTED>",
        input_cost_per_1m=3.0,    # $3 per 1M input tokens
        output_cost_per_1m=15.0,  # $15 per 1M output tokens
        max_tokens=8192,
        timeout=120,
        max_retries=3,
        rate_limit_per_minute=50
    ),
    
    # Claude Haiku 4.5 - Fast and efficient
    ModelType.CLAUDE_HAIKU: ModelConfig(
        name="claude-haiku-4-5-20251001",
        api_key="<REDACTED>",
        input_cost_per_1m=0.25,   # $0.25 per 1M input tokens
        output_cost_per_1m=1.25,  # $1.25 per 1M output tokens
        max_tokens=8192,
        timeout=60,
        max_retries=3,
        rate_limit_per_minute=100
    ),
    
    # DeepSeek Pro - Best for coding, affordable
    ModelType.DEEPSEEK_PRO: ModelConfig(
        name="deepseek-v4-pro",
        api_key="<REDACTED>",  # Get from https://platform.deepseek.com
        base_url="https://api.deepseek.com/v1",
        input_cost_per_1m=0.435,  # $0.435 per 1M input tokens
        output_cost_per_1m=0.87,  # $0.87 per 1M output tokens
        max_tokens=4096,
        timeout=90,
        max_retries=3,
        rate_limit_per_minute=60
    ),
    
    # DeepSeek Flash - General purpose, very cheap
    ModelType.DEEPSEEK_FLASH: ModelConfig(
        name="deepseek-v4-flash",
        api_key="<REDACTED>",
        base_url="https://api.deepseek.com/v1",
        input_cost_per_1m=0.14,
        output_cost_per_1m=0.28,
        max_tokens=4096,
        timeout=90,
        max_retries=3,
        rate_limit_per_minute=60
    ),
}

# =============================================================================
# Routing Configuration
# =============================================================================

# Conservative Strategy - Prioritize quality
CONSERVATIVE_ROUTING = RoutingConfig(
    deepseek_max_complexity=1,    # Only trivial tasks to DeepSeek
    haiku_max_complexity=2,        # Simple tasks to Haiku
    sonnet_max_complexity=4,       # Most tasks to Sonnet
    enable_caching=True,
    cache_ttl_seconds=3600,
    enable_cost_tracking=True,
    enable_fallback=True,
    fallback_on_error=True,
    max_concurrent_requests=10,
    request_timeout=120
)

# Balanced Strategy - Good mix of quality and cost
BALANCED_ROUTING = RoutingConfig(
    deepseek_max_complexity=2,    # Simple tasks to DeepSeek
    haiku_max_complexity=3,        # Moderate tasks to Haiku
    sonnet_max_complexity=4,       # Complex tasks to Sonnet
    enable_caching=True,
    cache_ttl_seconds=7200,
    enable_cost_tracking=True,
    enable_fallback=True,
    fallback_on_error=True,
    max_concurrent_requests=15,
    request_timeout=120
)

# Aggressive Cost Savings - Minimize costs
COST_OPTIMIZED_ROUTING = RoutingConfig(
    deepseek_max_complexity=3,    # Most tasks to DeepSeek
    haiku_max_complexity=4,        # Complex tasks to Haiku
    sonnet_max_complexity=5,       # Only critical to Sonnet
    enable_caching=True,
    cache_ttl_seconds=14400,       # 4 hours cache
    enable_cost_tracking=True,
    enable_fallback=True,
    fallback_on_error=True,
    max_concurrent_requests=20,
    request_timeout=90
)

# Development Strategy - Fast iteration, no caching
DEV_ROUTING = RoutingConfig(
    deepseek_max_complexity=2,
    haiku_max_complexity=3,
    sonnet_max_complexity=4,
    enable_caching=False,          # Fresh responses every time
    enable_cost_tracking=True,
    enable_fallback=True,
    fallback_on_error=True,
    max_concurrent_requests=5,
    request_timeout=60
)

# =============================================================================
# Usage Examples
# =============================================================================

"""
# Import the config
from config_example import MODEL_CONFIGS, BALANCED_ROUTING
from ai_router import AIRouter

# Create router with balanced strategy
router = AIRouter(MODEL_CONFIGS, BALANCED_ROUTING)

# Or use a different strategy
router = AIRouter(MODEL_CONFIGS, COST_OPTIMIZED_ROUTING)

# Or customize
from ai_router import RoutingConfig
custom_routing = RoutingConfig(
    deepseek_max_complexity=2,
    enable_caching=True,
    cache_ttl_seconds=3600
)
router = AIRouter(MODEL_CONFIGS, custom_routing)
"""

# =============================================================================
# Environment-specific Configurations
# =============================================================================

# Production: Quality first
PRODUCTION_CONFIG = {
    'models': MODEL_CONFIGS,
    'routing': CONSERVATIVE_ROUTING
}

# Staging: Balanced
STAGING_CONFIG = {
    'models': MODEL_CONFIGS,
    'routing': BALANCED_ROUTING
}

# Development: Cost savings
DEVELOPMENT_CONFIG = {
    'models': MODEL_CONFIGS,
    'routing': DEV_ROUTING
}

# =============================================================================
# Cost Estimates
# =============================================================================

"""
با BALANCED_ROUTING و 500 request در روز:

Breakdown (تقریبی):
- 40% DeepSeek (200 requests): ~$0.60/day
- 30% Haiku (150 requests): ~$2.50/day
- 25% Sonnet (125 requests): ~$6.25/day
- 5% Opus (25 requests): ~$3.00/day

Total daily: ~$12.35
Total monthly: ~$370

مقایسه با All-Opus:
- All Opus: $825/month
- Balanced: $370/month
- Savings: $455/month (55% کمتر)
"""
