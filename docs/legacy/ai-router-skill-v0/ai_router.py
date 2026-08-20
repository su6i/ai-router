# FROZEN REFERENCE — not executed.
# Origin: agent-constitution skills/ai-router @ 9b2be20 (2026-07-01).
# See docs/legacy/README.md.
"""
Professional AI Model Router
Intelligently routes requests between Claude Opus 4.6 and DeepSeek
with cost optimization, caching, monitoring, and fallback strategies.
"""

import asyncio
import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import httpx

# anthropic is OPTIONAL: only ClaudeClient needs it. The grunt-work providers
# (DeepSeek/MiniMax/Grok) go through OpenAICompatibleClient (httpx only), so a
# subscription-only setup with no Anthropic API key can run the router without
# installing anthropic. Import lazily and guard at use.
try:
    import anthropic
except ModuleNotFoundError:
    anthropic = None


# ============================================================================
# Configuration & Types
# ============================================================================

class ModelType(Enum):
    """Available AI models (updated 2026-06-30; Claude IDs/prices per claude-api skill)"""
    CLAUDE_OPUS = "claude-opus-4-8"
    CLAUDE_SONNET = "claude-sonnet-4-6"
    CLAUDE_HAIKU = "claude-haiku-4-5"
    MINIMAX = "MiniMax-M3"                 # minimax.io current model (set prefer_models to try it first)
    DEEPSEEK_FLASH = "deepseek-v4-flash"   # deepseek-chat/-reasoner are deprecated — use v4 names (active now)
    DEEPSEEK_PRO = "deepseek-v4-pro"       # complex tasks ($0.435 in / $0.87 out per 1M)
    GROK = "grok-4.3"                      # xAI Grok flagship ($1.25/$2.50 per 1M; fast: grok-build-0.1)
    OPENAI = "gpt-5.4"                     # OpenAI ($2.50/$15 per 1M; cheaper: gpt-5.4-mini $0.75/$4.50)


class TaskComplexity(Enum):
    """Task complexity levels for routing decisions"""
    TRIVIAL = 1      # Boilerplate, simple formatting
    SIMPLE = 2       # Basic CRUD, standard patterns
    MODERATE = 3     # Business logic, multi-file changes
    COMPLEX = 4      # Architecture decisions, optimization
    CRITICAL = 5     # Production issues, security-critical


@dataclass
class ModelConfig:
    """Configuration for a specific model"""
    name: str
    api_key: str
    base_url: Optional[str] = None
    input_cost_per_1m: float = 0.0  # USD per 1M tokens
    output_cost_per_1m: float = 0.0
    max_tokens: int = 8192
    timeout: int = 120
    max_retries: int = 3
    rate_limit_per_minute: int = 50


@dataclass
class RoutingConfig:
    """Configuration for routing logic"""
    # Complexity thresholds for model selection
    deepseek_max_complexity: int = 2
    haiku_max_complexity: int = 3
    sonnet_max_complexity: int = 4

    # Cost optimization
    enable_caching: bool = True
    cache_ttl_seconds: int = 3600
    enable_cost_tracking: bool = True

    # Provider preference — models to try first (in order) for non-critical tasks,
    # e.g. to spend prepaid credit on one provider before cheaper pay-as-you-go models.
    # Leave empty in shared/example configs; set it in your PERSONAL config only.
    prefer_models: tuple = ()

    # ── Plan/Act role routing ───────────────────────────────────────────────
    # Generic role → ordered model priority list.  Empty = fall back to
    # complexity routing.  Supported role names (convention, not enforced):
    # "planning" and "acting".  Personal configs may add more roles.
    # NEUTRAL DEFAULTS — do NOT put personal model choices here.
    #
    # Example personal config:
    #   roles = {
    #       "planning": (ModelType.CLAUDE_OPUS, ModelType.CLAUDE_SONNET),
    #       "acting":   (ModelType.DEEPSEEK_PRO, ModelType.MINIMAX, ModelType.GROK),
    #   }
    #
    # On every generate() call pass role="planning" or role="acting" (via kwargs
    # or context["role"]) to activate role-based selection.  If the primary role
    # model is unavailable / circuit-open the next in the tuple is tried before
    # complexity routing kicks in as the final fallback.
    roles: dict = field(default_factory=dict)

    # Fallback strategy
    enable_fallback: bool = True
    fallback_on_error: bool = True

    # Performance
    max_concurrent_requests: int = 10
    request_timeout: int = 120

    # ── Technique 2: effort mapping (Claude extended-thinking) ──────────────
    # Maps TaskComplexity int value → effort string ("low" | "medium" | "high").
    # Empty dict = disabled (no output_config sent).  Personal configs may set
    # e.g. {1: "low", 2: "low", 3: "medium", 4: "high"}.
    complexity_to_effort: Dict[int, str] = field(default_factory=dict)

    # ── Technique 3: prompt cache ───────────────────────────────────────────
    # True (default) = mark stable system prefix with cache_control.
    # Set False to disable globally (e.g. for cost analysis A/B tests).
    enable_prompt_cache: bool = True

    # ── Technique 4: batch queue ────────────────────────────────────────────
    # Flush non-urgent jobs to Claude Batches / DeepSeek batch every N seconds
    # or when batch_max items accumulate.  0 = disabled.
    batch_flush_seconds: int = 0
    batch_max: int = 100

    # ── Technique 5: off-peak (DeepSeek) ───────────────────────────────────
    # List of [start_utc_hour, end_utc_hour] windows where pricing is higher.
    # Example (DeepSeek peak 2026-07): [[1, 4], [6, 10]]
    # Empty list = no peak windows (default, neutral).
    peak_windows_utc: List[List[int]] = field(default_factory=list)
    # Cost multiplier applied during peak windows (default 1.0 = no surcharge).
    peak_multiplier: float = 1.0

    # ── Technique 6: max_tokens + context editing ───────────────────────────
    # "estimate" = derive max_tokens from prompt length heuristic.
    # "fixed"    = use ModelConfig.max_tokens as-is (legacy behaviour).
    max_tokens_policy: str = "fixed"
    # Enable Claude context-window editing (clear_tool_uses_20250919) for long
    # agentic sessions so input tokens don't balloon.  False by default.
    enable_context_editing: bool = False


@dataclass
class RequestMetrics:
    """Metrics for a single request"""
    model_used: ModelType
    complexity_score: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    cache_hit: bool
    timestamp: datetime = field(default_factory=datetime.now)
    error: Optional[str] = None


# ============================================================================
# Complexity Analyzer
# ============================================================================

class ComplexityAnalyzer:
    """Analyzes task complexity to determine appropriate model"""
    
    # Keywords indicating complexity levels
    COMPLEXITY_INDICATORS = {
        TaskComplexity.TRIVIAL: [
            'format', 'indent', 'comment', 'rename', 'import',
            'boilerplate', 'template', 'scaffold'
        ],
        TaskComplexity.SIMPLE: [
            'crud', 'getter', 'setter', 'validate', 'parse',
            'convert', 'map', 'filter', 'simple test'
        ],
        TaskComplexity.MODERATE: [
            'implement', 'refactor', 'optimize', 'business logic',
            'api endpoint', 'database', 'integration', 'middleware'
        ],
        TaskComplexity.COMPLEX: [
            'architecture', 'design pattern', 'algorithm',
            'performance', 'scalability', 'security', 'complex test',
            'distributed', 'concurrency', 'async'
        ],
        TaskComplexity.CRITICAL: [
            'bug fix production', 'security vulnerability', 'data loss',
            'critical bug', 'emergency', 'zero-day', 'exploit'
        ]
    }
    
    @staticmethod
    def analyze(prompt: str, context: Optional[Dict[str, Any]] = None) -> Tuple[TaskComplexity, float]:
        """
        Analyze prompt complexity
        
        Returns:
            Tuple of (TaskComplexity, confidence_score)
        """
        prompt_lower = prompt.lower()
        scores = {complexity: 0.0 for complexity in TaskComplexity}
        
        # Keyword-based scoring
        for complexity, keywords in ComplexityAnalyzer.COMPLEXITY_INDICATORS.items():
            for keyword in keywords:
                if keyword in prompt_lower:
                    scores[complexity] += 1.0
        
        # Context-based adjustments
        if context:
            # Large codebases increase complexity
            if context.get('file_count', 0) > 10:
                scores[TaskComplexity.COMPLEX] += 1.0
            
            # Production environment increases complexity
            if context.get('environment') == 'production':
                scores[TaskComplexity.CRITICAL] += 2.0
            
            # Time pressure increases complexity
            if context.get('urgent'):
                scores[TaskComplexity.CRITICAL] += 1.0
        
        # Length-based complexity (longer prompts often = more complex)
        word_count = len(prompt.split())
        if word_count > 200:
            scores[TaskComplexity.COMPLEX] += 0.5
        elif word_count > 100:
            scores[TaskComplexity.MODERATE] += 0.5
        
        # Determine final complexity
        if not any(scores.values()):
            return TaskComplexity.SIMPLE, 0.5  # Default
        
        max_complexity = max(scores, key=scores.get)
        confidence = min(scores[max_complexity] / 3.0, 1.0)  # Normalize to 0-1
        
        return max_complexity, confidence


# ============================================================================
# Cache Manager
# ============================================================================

class CacheManager:
    """Manages response caching to reduce costs"""
    
    def __init__(self, ttl_seconds: int = 3600):
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[str, Tuple[Any, datetime]] = {}
        self._lock = asyncio.Lock()
    
    def _get_cache_key(self, prompt: str, model: ModelType, **kwargs) -> str:
        """Generate cache key from request parameters"""
        cache_data = f"{prompt}:{model.value}:{json.dumps(kwargs, sort_keys=True)}"
        return hashlib.sha256(cache_data.encode()).hexdigest()
    
    async def get(self, prompt: str, model: ModelType, **kwargs) -> Optional[Any]:
        """Retrieve cached response if available and fresh"""
        async with self._lock:
            cache_key = self._get_cache_key(prompt, model, **kwargs)
            if cache_key in self._cache:
                response, timestamp = self._cache[cache_key]
                if datetime.now() - timestamp < timedelta(seconds=self.ttl_seconds):
                    return response
                else:
                    # Expired, remove from cache
                    del self._cache[cache_key]
            return None
    
    async def set(self, prompt: str, model: ModelType, response: Any, **kwargs):
        """Store response in cache"""
        async with self._lock:
            cache_key = self._get_cache_key(prompt, model, **kwargs)
            self._cache[cache_key] = (response, datetime.now())
    
    async def clear_expired(self):
        """Remove expired entries from cache"""
        async with self._lock:
            now = datetime.now()
            expired_keys = [
                key for key, (_, timestamp) in self._cache.items()
                if now - timestamp >= timedelta(seconds=self.ttl_seconds)
            ]
            for key in expired_keys:
                del self._cache[key]
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        return {
            'total_entries': len(self._cache),
            'memory_usage_mb': len(str(self._cache)) / (1024 * 1024)
        }


# ============================================================================
# Cost Tracker
# ============================================================================

class CostTracker:
    """Tracks API usage costs and provides analytics"""
    
    def __init__(self):
        self._metrics: List[RequestMetrics] = []
        self._lock = asyncio.Lock()
    
    async def record(self, metrics: RequestMetrics):
        """Record a request's metrics"""
        async with self._lock:
            self._metrics.append(metrics)
    
    async def get_summary(self, time_window: Optional[timedelta] = None) -> Dict[str, Any]:
        """Get cost summary for specified time window"""
        async with self._lock:
            if time_window:
                cutoff = datetime.now() - time_window
                metrics = [m for m in self._metrics if m.timestamp >= cutoff]
            else:
                metrics = self._metrics
            
            if not metrics:
                return {'total_requests': 0, 'total_cost': 0.0}
            
            total_cost = sum(m.cost_usd for m in metrics)
            total_requests = len(metrics)
            cache_hits = sum(1 for m in metrics if m.cache_hit)
            errors = sum(1 for m in metrics if m.error)
            
            # Per-model breakdown
            model_costs = {}
            for model in ModelType:
                model_metrics = [m for m in metrics if m.model_used == model]
                if model_metrics:
                    model_costs[model.value] = {
                        'requests': len(model_metrics),
                        'cost': sum(m.cost_usd for m in model_metrics),
                        'avg_latency_ms': sum(m.latency_ms for m in model_metrics) / len(model_metrics)
                    }
            
            return {
                'total_requests': total_requests,
                'total_cost': round(total_cost, 4),
                'cache_hit_rate': round(cache_hits / total_requests, 2) if total_requests else 0,
                'error_rate': round(errors / total_requests, 2) if total_requests else 0,
                'avg_cost_per_request': round(total_cost / total_requests, 4) if total_requests else 0,
                'model_breakdown': model_costs,
                'time_window': str(time_window) if time_window else 'all_time'
            }
    
    async def estimate_monthly_cost(self, requests_per_day: int) -> Dict[str, float]:
        """Estimate monthly cost based on recent usage patterns"""
        async with self._lock:
            if not self._metrics:
                return {'estimated_monthly_cost': 0.0}
            
            recent_metrics = self._metrics[-100:]  # Last 100 requests
            avg_cost = sum(m.cost_usd for m in recent_metrics) / len(recent_metrics)
            
            daily_cost = avg_cost * requests_per_day
            monthly_cost = daily_cost * 30
            
            return {
                'avg_cost_per_request': round(avg_cost, 4),
                'estimated_daily_cost': round(daily_cost, 2),
                'estimated_monthly_cost': round(monthly_cost, 2)
            }


# ============================================================================
# Model Clients
# ============================================================================

class ModelClient(ABC):
    """Abstract base class for model clients"""
    
    def __init__(self, config: ModelConfig):
        self.config = config
    
    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> Tuple[str, int, int]:
        """
        Generate response from model
        
        Returns:
            Tuple of (response_text, input_tokens, output_tokens)
        """
        pass
    
    def calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Calculate cost for token usage"""
        input_cost = (input_tokens / 1_000_000) * self.config.input_cost_per_1m
        output_cost = (output_tokens / 1_000_000) * self.config.output_cost_per_1m
        return input_cost + output_cost


class ClaudeClient(ModelClient):
    """Client for Anthropic Claude models"""

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        if anthropic is None:
            raise ImportError(
                "The 'anthropic' package is required for Claude models but is not "
                "installed. Either `uv pip install anthropic`, or route only to "
                "OpenAI-compatible providers (DeepSeek/MiniMax/Grok) and remove "
                "Claude models from your config."
            )
        self.client = anthropic.AsyncAnthropic(
            api_key=config.api_key,
            timeout=config.timeout
        )

    async def generate(self, prompt: str, **kwargs) -> Tuple[str, int, int]:
        """Generate response using Claude API (with prompt-cache read + effort mapping)."""
        max_tokens = kwargs.get('max_tokens', self.config.max_tokens)
        system = kwargs.get('system', None)
        enable_prompt_cache = kwargs.get('enable_prompt_cache', True)

        messages = [{"role": "user", "content": prompt}]

        # Prompt caching: mark the stable system prefix as cacheable so repeated calls
        # bill it at ~0.1x (read) instead of full input price. Keep `system` byte-stable
        # (no timestamps/UUIDs) and put volatile content in the user message.
        system_param = anthropic.NOT_GIVEN
        if system:
            if enable_prompt_cache:
                system_param = [{
                    "type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"},
                }]
            else:
                system_param = [{"type": "text", "text": system}]

        # Opus 4.7/4.8 reject sampling params (400) — only send temperature otherwise.
        extra: Dict[str, Any] = {}
        if not self.config.name.startswith(("claude-opus-4-8", "claude-opus-4-7")):
            extra["temperature"] = kwargs.get('temperature', 1.0)

        # Technique 2: effort mapping → output_config (extended thinking budget).
        # Passed via kwargs['output_config'] by AIRouter.generate() when configured.
        output_config = kwargs.get('output_config')
        if output_config:
            extra["output_config"] = output_config

        # Technique 6: context editing — strip completed tool-use blocks from history
        # to prevent input-token ballooning in long agentic sessions.
        if kwargs.get('enable_context_editing'):
            extra["betas"] = ["clear_tool_uses_20250919"]

        response = await self.client.messages.create(
            model=self.config.name,
            max_tokens=max_tokens,
            messages=messages,
            system=system_param,
            **extra,
        )

        response_text = response.content[0].text
        u = response.usage
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        # Bill cache reads at ~0.1x and writes at ~1.25x of input price by folding them
        # into an effective input-token count, so calculate_cost() stays accurate.
        effective_input = int(u.input_tokens + cache_read * 0.1 + cache_write * 1.25)
        if cache_read:
            logging.getLogger('AIRouter').info(
                f"prompt cache: {cache_read} read, {cache_write} written")

        return response_text, effective_input, u.output_tokens


class DeepSeekClient(ModelClient):
    """Client for DeepSeek models"""
    
    def __init__(self, config: ModelConfig):
        super().__init__(config)
        self.client = httpx.AsyncClient(
            base_url=config.base_url or "https://api.deepseek.com/v1",
            timeout=config.timeout,
            headers={"Authorization": f"Bearer {config.api_key}"}
        )
    
    async def generate(self, prompt: str, **kwargs) -> Tuple[str, int, int]:
        """Generate response using DeepSeek API"""
        max_tokens = kwargs.get('max_tokens', self.config.max_tokens)
        temperature = kwargs.get('temperature', 1.0)
        
        payload = {
            "model": self.config.name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature
        }
        
        response = await self.client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()
        
        response_text = data['choices'][0]['message']['content']
        input_tokens = data['usage']['prompt_tokens']
        output_tokens = data['usage']['completion_tokens']
        
        return response_text, input_tokens, output_tokens
    
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()


class OpenAICompatibleClient(ModelClient):
    """Generic client for any OpenAI-compatible /chat/completions endpoint.

    Covers:
    - Grok    (base_url="https://api.x.ai/v1",       model=ModelType.GROK.value)
    - OpenAI  (base_url="https://api.openai.com/v1",  model=ModelType.OPENAI.value)
    - MiniMax (base_url="https://api.minimax.io/v1",  model=ModelType.MINIMAX.value)

    Wire new providers by creating a ModelConfig with the appropriate base_url and
    api_key — no new client class needed.
    """

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        base_url = config.base_url or "https://api.openai.com/v1"
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=config.timeout,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def generate(self, prompt: str, **kwargs) -> Tuple[str, int, int]:
        """Send a chat completion request to the configured endpoint."""
        max_tokens = kwargs.get("max_tokens", self.config.max_tokens)
        temperature = kwargs.get("temperature", 1.0)

        payload: Dict[str, Any] = {
            "model": self.config.name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        # Some providers reject temperature=1.0 — only send if explicitly set.
        if "temperature" in kwargs:
            payload["temperature"] = temperature

        system = kwargs.get("system")
        if system:
            payload["messages"].insert(0, {"role": "system", "content": system})

        response = await self.client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()

        response_text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

        return response_text, input_tokens, output_tokens

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()


# ============================================================================
# Circuit Breaker
# ============================================================================

class CircuitBreaker:
    """Circuit breaker pattern for handling service failures"""
    
    def __init__(self, failure_threshold: int = 5, timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = 'closed'  # closed, open, half_open
    
    def record_success(self):
        """Record successful request"""
        self.failure_count = 0
        self.state = 'closed'
    
    def record_failure(self):
        """Record failed request"""
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.failure_count >= self.failure_threshold:
            self.state = 'open'
    
    def can_request(self) -> bool:
        """Check if requests are allowed"""
        if self.state == 'closed':
            return True
        
        if self.state == 'open':
            if time.time() - self.last_failure_time > self.timeout:
                self.state = 'half_open'
                return True
            return False
        
        # half_open state
        return True


# ============================================================================
# AI Router
# ============================================================================

class AIRouter:
    """
    Professional AI Router that intelligently routes requests
    between multiple AI models with cost optimization
    """

    def __init__(
        self,
        model_configs: Dict[ModelType, ModelConfig],
        routing_config: Optional[RoutingConfig] = None
    ):
        self.model_configs = model_configs
        self.routing_config = routing_config or RoutingConfig()

        # Initialize clients
        self.clients: Dict[ModelType, ModelClient] = {}
        self._initialize_clients()
        # Note: DEEPSEEK_FLASH supports thinking mode via extra_body={"thinking": {"type": "enabled", "budget_tokens": N}}

        # Initialize components
        self.cache = CacheManager(self.routing_config.cache_ttl_seconds) if self.routing_config.enable_caching else None
        self.cost_tracker = CostTracker() if self.routing_config.enable_cost_tracking else None
        self.complexity_analyzer = ComplexityAnalyzer()

        # Circuit breakers per model
        self.circuit_breakers = {model: CircuitBreaker() for model in ModelType}

        # Semaphore for concurrency control
        self.semaphore = asyncio.Semaphore(self.routing_config.max_concurrent_requests)

        # Logging
        self._setup_logging()

        # Technique 5: emit local-timezone peak warning on startup
        self._log_peak_warning()
    
    # ModelTypes handled by OpenAICompatibleClient (base_url from ModelConfig.base_url)
    _OPENAI_COMPAT_TYPES = frozenset([
        ModelType.GROK,
        ModelType.OPENAI,
        ModelType.MINIMAX,
    ])

    def _initialize_clients(self):
        """Initialize model clients based on configuration."""
        for model_type, config in self.model_configs.items():
            if model_type in (ModelType.CLAUDE_OPUS, ModelType.CLAUDE_SONNET, ModelType.CLAUDE_HAIKU):
                self.clients[model_type] = ClaudeClient(config)
            elif model_type in (ModelType.DEEPSEEK_FLASH, ModelType.DEEPSEEK_PRO):
                self.clients[model_type] = DeepSeekClient(config)
            elif model_type in self._OPENAI_COMPAT_TYPES:
                # Generic OpenAI-compatible endpoint (Grok, OpenAI, MiniMax, …)
                self.clients[model_type] = OpenAICompatibleClient(config)
    
    def _setup_logging(self):
        """Setup logging configuration"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger('AIRouter')

    # ── Technique 5: peak-window helpers ────────────────────────────────────

    def is_peak(self) -> bool:
        """Return True if current UTC time falls inside any configured peak window."""
        windows = self.routing_config.peak_windows_utc
        if not windows:
            return False
        utc_hour = datetime.now(timezone.utc).hour
        for window in windows:
            start, end = window[0], window[1]
            if start <= end:
                if start <= utc_hour < end:
                    return True
            else:
                # wraps midnight, e.g. [22, 2]
                if utc_hour >= start or utc_hour < end:
                    return True
        return False

    def _log_peak_warning(self) -> None:
        """Log peak windows in the machine's local timezone on startup (stdlib only)."""
        windows = self.routing_config.peak_windows_utc
        if not windows or self.routing_config.peak_multiplier == 1.0:
            return
        # DST-aware local UTC offset for "now" — no extra dependency.
        offset = datetime.now().astimezone().utcoffset() or timedelta(0)
        offset_h = round(offset.total_seconds() / 3600)

        def _local(utc_hour: int) -> str:
            return f"{(utc_hour + offset_h) % 24:02d}:00"

        windows_str = " & ".join(f"{_local(w[0])}–{_local(w[1])}" for w in windows)
        self.logger.warning(
            f"DeepSeek peak in YOUR local time: {windows_str} "
            f"(×{self.routing_config.peak_multiplier} cost) — batchable jobs deferred outside peak."
        )
    
    def select_model(
        self,
        prompt: str,
        context: Optional[Dict[str, Any]] = None,
        force_model: Optional[ModelType] = None,
        role: Optional[str] = None,
    ) -> ModelType:
        """
        Select appropriate model based on task complexity (or role if provided).

        Args:
            prompt: The user's prompt
            context: Additional context for complexity analysis
            force_model: Override automatic selection
            role: Optional role name ("planning", "acting", or any custom key).
                  If set and present in routing_config.roles, the role's ordered
                  model list is tried before complexity routing.

        Returns:
            Selected ModelType
        """
        if force_model:
            return force_model

        # ── Role-based routing (highest priority after force_model) ─────────
        # Resolve role from explicit arg or from context dict.
        resolved_role = role or (context or {}).get("role")
        if resolved_role and resolved_role in self.routing_config.roles:
            for m in self.routing_config.roles[resolved_role]:
                if m in self.clients and self.circuit_breakers[m].can_request():
                    self.logger.info(
                        f"Role '{resolved_role}' → {m.value}"
                    )
                    return m
            # All role models unavailable → fall through to complexity routing.
            self.logger.warning(
                f"Role '{resolved_role}': no available model in role list, "
                "falling back to complexity routing."
            )

        complexity, confidence = self.complexity_analyzer.analyze(prompt, context)

        self.logger.info(
            f"Complexity analysis: {complexity.name} (confidence: {confidence:.2f})"
        )

        # Honor configured provider preference for non-critical tasks (generic — a user
        # might prefer a provider to spend prepaid credit). Critical tasks ignore it.
        if complexity.value <= self.routing_config.sonnet_max_complexity:
            for m in self.routing_config.prefer_models:
                if m in self.clients:
                    return m

        # Route based on complexity
        if complexity.value <= self.routing_config.deepseek_max_complexity:
            # DeepSeek is the cheapest pay-as-you-go tier: Flash for simple, Pro for moderate.
            if complexity.value <= 1 and ModelType.DEEPSEEK_FLASH in self.clients:
                return ModelType.DEEPSEEK_FLASH
            elif ModelType.DEEPSEEK_PRO in self.clients:
                return ModelType.DEEPSEEK_PRO

        if complexity.value <= self.routing_config.haiku_max_complexity:
            if ModelType.CLAUDE_HAIKU in self.clients:
                return ModelType.CLAUDE_HAIKU

        if complexity.value <= self.routing_config.sonnet_max_complexity:
            if ModelType.CLAUDE_SONNET in self.clients:
                return ModelType.CLAUDE_SONNET

        # Default to most powerful model for critical tasks
        if ModelType.CLAUDE_OPUS in self.clients:
            return ModelType.CLAUDE_OPUS

        # Fallback to any available model
        return next(iter(self.clients.keys()))
    
    async def generate(
        self,
        prompt: str,
        context: Optional[Dict[str, Any]] = None,
        force_model: Optional[ModelType] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Generate response with intelligent routing and fallback.

        Extra kwargs (all optional):
          system              — stable system prompt (prompt-cached on Claude)
          temperature         — sampling temperature
          max_tokens          — override max tokens
          is_urgent           — bool; non-urgent DeepSeek jobs may be deferred off-peak
          output_config       — dict, e.g. {"effort": "high"} (passed through to Claude)
          role                — "planning" | "acting" | any key in routing_config.roles;
                                activates role-based model selection (tried before
                                complexity routing).
        """
        async with self.semaphore:
            start_time = time.time()

            # Extract role (consumed here, not forwarded to the model client)
            role = kwargs.pop("role", None)

            # Determine selected model
            selected_model = self.select_model(prompt, context, force_model, role=role)

            # Check cache first
            if self.cache:
                cached_response = await self.cache.get(prompt, selected_model, **kwargs)
                if cached_response:
                    self.logger.info(f"Cache hit for model {selected_model.value}")
                    return {
                        **cached_response,
                        'cache_hit': True,
                        'latency_ms': 0
                    }

            # Technique 2: inject effort from complexity mapping (Claude only)
            if (self.routing_config.complexity_to_effort
                    and selected_model in (
                        ModelType.CLAUDE_OPUS, ModelType.CLAUDE_SONNET, ModelType.CLAUDE_HAIKU)
                    and 'output_config' not in kwargs):
                complexity, _ = self.complexity_analyzer.analyze(prompt, context)
                effort = self.routing_config.complexity_to_effort.get(complexity.value)
                if effort:
                    kwargs['output_config'] = {"effort": effort}

            # Technique 3: propagate enable_prompt_cache flag
            if 'enable_prompt_cache' not in kwargs:
                kwargs['enable_prompt_cache'] = self.routing_config.enable_prompt_cache

            # Technique 6: propagate enable_context_editing flag
            if 'enable_context_editing' not in kwargs:
                kwargs['enable_context_editing'] = self.routing_config.enable_context_editing

            # Technique 6: max_tokens_policy = "estimate" → heuristic based on prompt length
            if self.routing_config.max_tokens_policy == "estimate" and 'max_tokens' not in kwargs:
                word_count = len(prompt.split())
                # Heuristic: output ≈ 1× prompt words, capped at model max; floor 256.
                estimated = max(256, min(word_count * 2, self.model_configs[selected_model].max_tokens))
                kwargs['max_tokens'] = estimated

            # Try primary model with circuit breaker (applies peak_multiplier in cost)
            response_data = await self._generate_with_fallback(
                prompt, selected_model, start_time, role=role, **kwargs
            )

            # Cache the response
            if self.cache and not response_data.get('error'):
                await self.cache.set(prompt, selected_model, response_data, **kwargs)

            return response_data
    
    async def _generate_with_fallback(
        self,
        prompt: str,
        primary_model: ModelType,
        start_time: float,
        role: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Generate with automatic fallback on failure."""
        models_to_try = [primary_model]

        # Add fallback models if enabled
        if self.routing_config.enable_fallback:
            # Role fallbacks: remaining models in the role list (after the primary),
            # so the user's explicit priority order is honoured on error too.
            role_fallbacks: List[ModelType] = []
            if role and role in self.routing_config.roles:
                role_fallbacks = [
                    m for m in self.routing_config.roles[role]
                    if m != primary_model and m in self.clients
                ]

            # Preferred providers first (honors prefer_models, e.g. prepaid credit),
            # then cheapest-first pay-as-you-go so a primary error degrades cost-safely.
            generic_fallbacks = list(self.routing_config.prefer_models) + [
                ModelType.DEEPSEEK_FLASH,
                ModelType.DEEPSEEK_PRO,
                ModelType.CLAUDE_SONNET,
                ModelType.CLAUDE_HAIKU,
            ]
            # Role fallbacks take priority over generic fallbacks.
            combined = role_fallbacks + [
                m for m in generic_fallbacks if m not in role_fallbacks
            ]
            models_to_try.extend([m for m in combined if m != primary_model and m in self.clients])
        
        last_error = None
        
        for model in models_to_try:
            # Check circuit breaker
            if not self.circuit_breakers[model].can_request():
                self.logger.warning(f"Circuit breaker open for {model.value}, skipping")
                continue
            
            try:
                client = self.clients[model]
                response_text, input_tokens, output_tokens = await client.generate(
                    prompt, **kwargs
                )
                
                # Success - record metrics
                latency_ms = (time.time() - start_time) * 1000
                cost = client.calculate_cost(input_tokens, output_tokens)

                # Technique 5: apply peak surcharge to effective cost so the cost
                # tracker reflects real spend during DeepSeek peak windows.
                if (self.is_peak()
                        and model in (ModelType.DEEPSEEK_FLASH, ModelType.DEEPSEEK_PRO)
                        and self.routing_config.peak_multiplier != 1.0):
                    cost *= self.routing_config.peak_multiplier

                self.circuit_breakers[model].record_success()
                
                metrics = RequestMetrics(
                    model_used=model,
                    complexity_score=0.0,  # Filled by caller
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    cache_hit=False
                )
                
                if self.cost_tracker:
                    await self.cost_tracker.record(metrics)
                
                self.logger.info(
                    f"Generated with {model.value}: {input_tokens}+{output_tokens} tokens, "
                    f"${cost:.4f}, {latency_ms:.0f}ms"
                )
                
                return {
                    'response': response_text,
                    'model': model.value,
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                    'cost_usd': cost,
                    'latency_ms': latency_ms,
                    'cache_hit': False
                }
            
            except Exception as e:
                self.logger.error(f"Error with {model.value}: {str(e)}")
                self.circuit_breakers[model].record_failure()
                last_error = e
                
                if not self.routing_config.fallback_on_error:
                    raise
                
                continue
        
        # All models failed
        raise Exception(f"All models failed. Last error: {str(last_error)}")
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get router statistics"""
        stats = {
            'active_models': list(self.clients.keys()),
        }
        
        if self.cost_tracker:
            stats['cost_summary'] = await self.cost_tracker.get_summary()
            stats['cost_summary_24h'] = await self.cost_tracker.get_summary(
                timedelta(hours=24)
            )
        
        if self.cache:
            stats['cache_stats'] = self.cache.get_stats()
        
        # Circuit breaker states
        stats['circuit_breakers'] = {
            model.value: breaker.state
            for model, breaker in self.circuit_breakers.items()
        }
        
        return stats
    
    async def cleanup(self):
        """Cleanup resources"""
        for client in self.clients.values():
            if hasattr(client, 'client') and hasattr(client.client, 'aclose'):
                await client.client.aclose()


# ============================================================================
# Example Usage & Configuration
# ============================================================================

async def main():
    """Example usage of AI Router"""
    
    # Configure models (replace with your actual API keys)
    model_configs = {
        ModelType.CLAUDE_OPUS: ModelConfig(
            name="claude-opus-4-8",
            api_key="<REDACTED>",
            input_cost_per_1m=5.0,
            output_cost_per_1m=25.0,
            max_tokens=8192
        ),
        ModelType.CLAUDE_SONNET: ModelConfig(
            name="claude-sonnet-4-6",
            api_key="<REDACTED>",
            input_cost_per_1m=3.0,
            output_cost_per_1m=15.0,
            max_tokens=8192
        ),
        ModelType.CLAUDE_HAIKU: ModelConfig(
            name="claude-haiku-4-5",
            api_key="<REDACTED>",
            input_cost_per_1m=1.0,
            output_cost_per_1m=5.0,
            max_tokens=8192
        ),
        ModelType.MINIMAX: ModelConfig(
            # minimax.io example provider. List price; M3 has a permanent 50% off.
            # To always try MiniMax first (e.g. to spend prepaid credit), set
            # RoutingConfig(prefer_models=(ModelType.MINIMAX,)) in your personal config.
            name="MiniMax-M3",
            api_key="<REDACTED>",
            base_url="https://api.minimax.io/v1",   # verify on platform.minimax.io
            input_cost_per_1m=0.30,
            output_cost_per_1m=1.20,
            max_tokens=16384
        ),
        ModelType.DEEPSEEK_FLASH: ModelConfig(
            # DeepSeek official price (api-docs.deepseek.com/quick_start/pricing):
            # input $0.14 cache-miss / $0.0028 cache-hit, output $0.28 per 1M.
            # ⚠️ DeepSeek peak/valley is active now: peak = 2× (UTC 01-04 & 06-10).
            name="deepseek-v4-flash",
            api_key="<REDACTED>",
            base_url="https://api.deepseek.com/v1",
            input_cost_per_1m=0.14,
            output_cost_per_1m=0.28,
            max_tokens=16384
        ),
        # ── OpenAI-compatible providers (optional, add any subset) ──────────
        ModelType.GROK: ModelConfig(
            # xAI Grok flagship (https://docs.x.ai/developers/pricing)
            name="grok-4.3",
            api_key="<REDACTED>",
            base_url="https://api.x.ai/v1",
            input_cost_per_1m=1.25,
            output_cost_per_1m=2.5,
            max_tokens=131072,
        ),
        ModelType.OPENAI: ModelConfig(
            # OpenAI (https://developers.openai.com/api/docs/pricing)
            name="gpt-5.4",
            api_key="<REDACTED>",
            base_url="https://api.openai.com/v1",
            input_cost_per_1m=2.5,
            output_cost_per_1m=15.0,
            max_tokens=32768,
        ),
    }
    
    # Configure routing
    routing_config = RoutingConfig(
        deepseek_max_complexity=2,
        enable_caching=True,
        enable_fallback=True,
        max_concurrent_requests=10
    )
    
    # Initialize router
    router = AIRouter(model_configs, routing_config)
    
    try:
        # Example 1: Simple task (should use DeepSeek)
        print("\\n=== Example 1: Simple CRUD ===")
        response1 = await router.generate(
            "Write a simple CRUD function for a user model in Python"
        )
        print(f"Model used: {response1['model']}")
        print(f"Cost: ${response1['cost_usd']:.4f}")
        print(f"Response preview: {response1['response'][:100]}...")
        
        # Example 2: Complex task (should use Claude Opus)
        print("\\n=== Example 2: Complex Architecture ===")
        response2 = await router.generate(
            "Design a distributed microservices architecture for a high-traffic "
            "e-commerce platform with considerations for scalability and fault tolerance",
            context={'environment': 'production', 'urgent': True}
        )
        print(f"Model used: {response2['model']}")
        print(f"Cost: ${response2['cost_usd']:.4f}")
        
        # Example 3: Force specific model
        print("\\n=== Example 3: Force Model ===")
        response3 = await router.generate(
            "Explain dependency injection",
            force_model=ModelType.CLAUDE_HAIKU
        )
        print(f"Model used: {response3['model']}")
        
        # Get statistics
        print("\\n=== Statistics ===")
        stats = await router.get_stats()
        print(json.dumps(stats, indent=2, default=str))
        
        # Estimate monthly costs
        if router.cost_tracker:
            monthly_estimate = await router.cost_tracker.estimate_monthly_cost(
                requests_per_day=500
            )
            print("\\n=== Monthly Cost Estimate (500 requests/day) ===")
            print(json.dumps(monthly_estimate, indent=2))
    
    finally:
        await router.cleanup()


if __name__ == "__main__":
    # Run the example
    asyncio.run(main())
