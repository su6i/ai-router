# FROZEN REFERENCE — not executed.
# Origin: agent-constitution skills/ai-router @ 9b2be20 (2026-07-01).
# See docs/legacy/README.md.
"""
AI Router — OpenAI-compatible FastAPI proxy (skill: ai-router, v2.0.0)

POST /v1/chat/completions  →  routes through AIRouter  →  OpenAI-shaped response
POST /v1/completions       →  same, legacy text endpoint
GET  /v1/models            →  list available models
GET  /health               →  readiness check

Usage:
    uvicorn server:app --host 0.0.0.0 --port 8787

Set AIROUTER_CONFIG_MODULE env var to a dotted import path of a module that
exports a `build() -> AIRouter` function (e.g. your personal vault config.py).
If unset, the server builds a minimal demo router from env-var API keys.

Cline integration:
    In Cline settings set  base_url = http://localhost:8787/v1
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ── Router engine path resolution ────────────────────────────────────────────
# When server.py is run from the skill directory, ai_router is a sibling.
_SKILL_DIR = Path(__file__).parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

from ai_router import AIRouter, ModelConfig, ModelType, RoutingConfig  # noqa: E402

logger = logging.getLogger("airouter.server")

# ── Global router instance (shared across all requests) ──────────────────────
_router: Optional[AIRouter] = None


def _build_default_router() -> AIRouter:
    """Build a minimal router from environment variables (demo / fallback)."""
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")

    models: Dict[ModelType, ModelConfig] = {}
    if anthropic_key:
        models[ModelType.CLAUDE_SONNET] = ModelConfig(
            name="claude-sonnet-4-6",
            api_key=anthropic_key,
            input_cost_per_1m=3.0,
            output_cost_per_1m=15.0,
            max_tokens=8192,
        )
        models[ModelType.CLAUDE_HAIKU] = ModelConfig(
            name="claude-haiku-4-5",
            api_key=anthropic_key,
            input_cost_per_1m=1.0,
            output_cost_per_1m=5.0,
            max_tokens=8192,
        )
    if deepseek_key:
        models[ModelType.DEEPSEEK_FLASH] = ModelConfig(
            name="deepseek-v4-flash",
            api_key=deepseek_key,
            base_url="https://api.deepseek.com/v1",
            input_cost_per_1m=0.14,
            output_cost_per_1m=0.28,
            max_tokens=16384,
        )
    if not models:
        # No keys found — router will fail at call time, not at startup.
        logger.warning("No API keys found in env. Set ANTHROPIC_API_KEY / DEEPSEEK_API_KEY.")

    routing = RoutingConfig(
        enable_caching=True,
        enable_fallback=bool(models),
        enable_prompt_cache=True,
        max_tokens_policy="fixed",
    )
    return AIRouter(models, routing)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build (or import) the router on startup and clean up on shutdown."""
    global _router

    config_module = os.getenv("AIROUTER_CONFIG_MODULE")
    if config_module:
        try:
            mod = importlib.import_module(config_module)
            _router = mod.build()
            logger.info(f"AIRouter loaded from module: {config_module}")
        except Exception as exc:
            logger.error(f"Failed to import {config_module}: {exc} — falling back to default")
            _router = _build_default_router()
    else:
        _router = _build_default_router()
        logger.info("AIRouter started with default env-var config.")

    yield

    if _router:
        await _router.cleanup()


app = FastAPI(
    title="AI Router Proxy",
    description="OpenAI-compatible proxy that routes to the cheapest capable model.",
    version="2.0.0",
    lifespan=lifespan,
)


# ── OpenAI request / response shapes ─────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None          # ignored — router picks the model
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    # Router-specific extensions (also accepted via X-Urgent / X-Project headers)
    x_urgent: Optional[bool] = Field(default=None, alias="x_urgent")
    x_project: Optional[str] = Field(default=None, alias="x_project")

    model_config = {"populate_by_name": True}


class CompletionRequest(BaseModel):
    """Legacy /v1/completions (text)."""
    model: Optional[str] = None
    prompt: str
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


def _messages_to_prompt(messages: List[ChatMessage]) -> tuple[str, Optional[str]]:
    """
    Extract (user_prompt, system_prompt) from a messages list.
    Concatenates multiple user/assistant turns into a single string for AIRouter.
    The stable system turn is extracted separately for prompt-caching.
    """
    system_parts: List[str] = []
    dialogue_parts: List[str] = []
    for m in messages:
        content = m.content if isinstance(m.content, str) else str(m.content)
        if m.role == "system":
            system_parts.append(content)
        elif m.role == "user":
            dialogue_parts.append(f"User: {content}")
        elif m.role == "assistant":
            dialogue_parts.append(f"Assistant: {content}")

    system = "\n\n".join(system_parts) if system_parts else None
    prompt = "\n\n".join(dialogue_parts) if dialogue_parts else "(empty)"
    return prompt, system


def _build_openai_response(
    router_result: Dict[str, Any],
    request_id: str,
) -> Dict[str, Any]:
    """Convert AIRouter result dict → OpenAI chat.completions shape."""
    now = int(time.time())
    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": now,
        "model": router_result.get("model", "unknown"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": router_result.get("response", ""),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": router_result.get("input_tokens", 0),
            "completion_tokens": router_result.get("output_tokens", 0),
            "total_tokens": (
                router_result.get("input_tokens", 0)
                + router_result.get("output_tokens", 0)
            ),
        },
        # Non-standard extension fields (cheap to include, useful for clients)
        "x_router": {
            "cost_usd": router_result.get("cost_usd", 0.0),
            "latency_ms": router_result.get("latency_ms", 0.0),
            "cache_hit": router_result.get("cache_hit", False),
        },
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> Dict[str, Any]:
    """Readiness probe."""
    if _router is None:
        raise HTTPException(status_code=503, detail="Router not initialized")
    stats = await _router.get_stats()
    return {"status": "ok", "models": [m.value for m in _router.clients], **stats}


@app.get("/v1/models")
async def list_models() -> Dict[str, Any]:
    """List available models (OpenAI-compatible)."""
    if _router is None:
        raise HTTPException(status_code=503, detail="Router not initialized")
    data = [
        {
            "id": model_type.value,
            "object": "model",
            "created": 1700000000,
            "owned_by": "ai-router",
        }
        for model_type in _router.clients
    ]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request) -> JSONResponse:
    """
    OpenAI-compatible chat completions endpoint.
    Accepts X-Urgent and X-Project headers for routing hints.
    """
    if _router is None:
        raise HTTPException(status_code=503, detail="Router not initialized")

    # Read routing hints from headers (override body fields)
    is_urgent_header = request.headers.get("x-urgent", "").lower()
    is_urgent = (
        req.x_urgent
        if req.x_urgent is not None
        else (is_urgent_header == "true" if is_urgent_header else True)
    )
    project = request.headers.get("x-project") or req.x_project

    prompt, system = _messages_to_prompt(req.messages)

    context: Dict[str, Any] = {"urgent": is_urgent}
    if project:
        context["project"] = project

    kwargs: Dict[str, Any] = {}
    if system:
        kwargs["system"] = system
    if req.temperature is not None:
        kwargs["temperature"] = req.temperature
    if req.max_tokens is not None:
        kwargs["max_tokens"] = req.max_tokens

    try:
        result = await _router.generate(prompt=prompt, context=context, **kwargs)
    except Exception as exc:
        logger.error(f"Router error: {exc}")
        raise HTTPException(status_code=502, detail=str(exc))

    request_id = uuid.uuid4().hex
    return JSONResponse(content=_build_openai_response(result, request_id))


@app.post("/v1/completions")
async def completions(req: CompletionRequest, request: Request) -> JSONResponse:
    """Legacy text completions endpoint (wraps chat)."""
    if _router is None:
        raise HTTPException(status_code=503, detail="Router not initialized")

    kwargs: Dict[str, Any] = {}
    if req.temperature is not None:
        kwargs["temperature"] = req.temperature
    if req.max_tokens is not None:
        kwargs["max_tokens"] = req.max_tokens

    try:
        result = await _router.generate(prompt=req.prompt, **kwargs)
    except Exception as exc:
        logger.error(f"Router error: {exc}")
        raise HTTPException(status_code=502, detail=str(exc))

    now = int(time.time())
    request_id = uuid.uuid4().hex
    return JSONResponse(content={
        "id": f"cmpl-{request_id}",
        "object": "text_completion",
        "created": now,
        "model": result.get("model", "unknown"),
        "choices": [
            {
                "text": result.get("response", ""),
                "index": 0,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": result.get("input_tokens", 0),
            "completion_tokens": result.get("output_tokens", 0),
            "total_tokens": result.get("input_tokens", 0) + result.get("output_tokens", 0),
        },
    })
