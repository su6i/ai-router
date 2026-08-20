# FROZEN REFERENCE — not executed.
# Origin: agent-constitution skills/ai-router @ 9b2be20 (2026-07-01).
# See docs/legacy/README.md.
"""
AI Router — Batch Queue (skill: ai-router, v2.0.0)

Non-urgent requests are collected here and flushed as a batch to reduce cost:
  - Claude: Anthropic Batches API (50% off list price)
  - DeepSeek: off-peak defer (held until is_peak() is False)

Usage (from server.py or any integration point):
    queue = BatchQueue(router, flush_seconds=60, batch_max=100)
    await queue.start()                            # start background flusher
    job_id = await queue.enqueue(prompt, **kwargs) # returns immediately
    result  = await queue.wait(job_id)             # block until done (optional)
    await queue.stop()

The queue is transparent: `enqueue()` / `wait()` mirror `AIRouter.generate()` but
the actual LLM call may be deferred by up to `flush_seconds`.

Design notes:
  - Only Claude models use the Anthropic Batches API (real 50% discount).
  - DeepSeek non-urgent jobs are held until is_peak() is False, then flushed
    as concurrent asyncio tasks (DeepSeek has no batch API; off-peak price IS
    the 1× price, peak is 2×, so deferring saves the multiplier).
  - If flush_seconds == 0 (config default), enqueue() calls AIRouter.generate()
    immediately (pass-through mode — no batching overhead).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("airouter.batch_queue")

# Import lazily so this module can be imported without anthropic installed.
try:
    import anthropic as _anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False


@dataclass
class _Job:
    job_id: str
    prompt: str
    kwargs: Dict[str, Any]
    future: asyncio.Future = field(default_factory=asyncio.Future)
    is_claude: bool = False  # True → use Anthropic Batches API


class BatchQueue:
    """
    Non-urgent request accumulator with configurable flush interval.

    Parameters
    ----------
    router : AIRouter
        The underlying router instance.
    flush_seconds : int
        How often (in seconds) to flush queued jobs.  0 = disabled (passthrough).
    batch_max : int
        Flush immediately when queue reaches this size.
    """

    def __init__(self, router: Any, flush_seconds: int = 0, batch_max: int = 100):
        self.router = router
        self.flush_seconds = flush_seconds
        self.batch_max = batch_max
        self._queue: list[_Job] = []
        self._lock = asyncio.Lock()
        self._flusher_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the background flusher coroutine (idempotent)."""
        if self.flush_seconds <= 0:
            return
        if self._flusher_task and not self._flusher_task.done():
            return
        self._flusher_task = asyncio.create_task(self._flusher_loop())
        logger.info(
            f"BatchQueue started: flush every {self.flush_seconds}s, max={self.batch_max}"
        )

    async def stop(self) -> None:
        """Flush remaining jobs and stop the flusher."""
        if self._flusher_task:
            self._flusher_task.cancel()
        await self._flush()

    async def enqueue(self, prompt: str, **kwargs) -> asyncio.Future:
        """
        Add a non-urgent job to the queue.  Returns a Future that resolves
        with the same dict that AIRouter.generate() would return.

        If flush_seconds == 0, the job is executed immediately (passthrough).
        """
        if self.flush_seconds <= 0:
            # Passthrough — no batching configured
            result = await self.router.generate(prompt, **kwargs)
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            fut.set_result(result)
            return fut

        job = _Job(
            job_id=uuid.uuid4().hex,
            prompt=prompt,
            kwargs=kwargs,
        )

        async with self._lock:
            self._queue.append(job)
            should_flush = len(self._queue) >= self.batch_max

        if should_flush:
            asyncio.create_task(self._flush())

        return job.future

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _flusher_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_seconds)
            await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            jobs = self._queue[:]
            self._queue.clear()

        if not jobs:
            return

        logger.info(f"BatchQueue: flushing {len(jobs)} jobs")

        # Separate into Claude-Batches-eligible vs. regular
        claude_jobs = [j for j in jobs if j.is_claude]
        other_jobs  = [j for j in jobs if not j.is_claude]

        if claude_jobs and _HAS_ANTHROPIC:
            await self._flush_claude_batch(claude_jobs)

        # Everything else (DeepSeek off-peak, MiniMax, etc.) — run concurrently
        if other_jobs:
            await asyncio.gather(*[self._run_job(j) for j in other_jobs],
                                 return_exceptions=True)

    async def _run_job(self, job: _Job) -> None:
        """Execute a single job via AIRouter and resolve its future."""
        try:
            # Off-peak gate: if this is a DeepSeek job and we're still in peak,
            # keep retrying every 60 s until is_peak() is False.
            from ai_router import ModelType
            is_deepseek_candidate = any(
                str(m).startswith(("deepseek",))
                for m in (self.router.routing_config.prefer_models or [])
            ) or (
                self.router.routing_config.peak_windows_utc
                and self.router.routing_config.peak_multiplier > 1.0
            )
            if is_deepseek_candidate:
                while self.router.is_peak():
                    logger.info("BatchQueue: DeepSeek job deferred — still in peak window.")
                    await asyncio.sleep(60)

            result = await self.router.generate(job.prompt, **job.kwargs)
            if not job.future.done():
                job.future.set_result(result)
        except Exception as exc:
            logger.error(f"BatchQueue job {job.job_id} failed: {exc}")
            if not job.future.done():
                job.future.set_exception(exc)

    async def _flush_claude_batch(self, jobs: list[_Job]) -> None:
        """
        Submit jobs to the Anthropic Message Batches API (50% discount).
        Falls back to sequential calls if the API is unavailable.
        Ref: https://docs.anthropic.com/en/docs/build-with-claude/message-batches
        """
        if not _HAS_ANTHROPIC:
            for job in jobs:
                await self._run_job(job)
            return

        # Find any Claude client to get its API key + model name
        from ai_router import ModelType
        claude_client = None
        for mt in (ModelType.CLAUDE_SONNET, ModelType.CLAUDE_HAIKU, ModelType.CLAUDE_OPUS):
            if mt in self.router.clients:
                claude_client = self.router.clients[mt]
                break

        if claude_client is None:
            for job in jobs:
                await self._run_job(job)
            return

        client = _anthropic.AsyncAnthropic(api_key=claude_client.config.api_key)

        batch_requests = []
        for job in jobs:
            max_tokens = job.kwargs.get('max_tokens', claude_client.config.max_tokens)
            system = job.kwargs.get('system')
            messages = [{"role": "user", "content": job.prompt}]
            req: Dict[str, Any] = {
                "custom_id": job.job_id,
                "params": {
                    "model": claude_client.config.name,
                    "max_tokens": max_tokens,
                    "messages": messages,
                },
            }
            if system:
                req["params"]["system"] = system
            batch_requests.append(req)

        try:
            batch = await client.messages.batches.create(requests=batch_requests)
            logger.info(
                f"BatchQueue: Anthropic batch {batch.id} submitted with {len(jobs)} requests"
            )

            # Poll until batch processing ends
            while batch.processing_status == "in_progress":
                await asyncio.sleep(10)
                batch = await client.messages.batches.retrieve(batch.id)

            # Collect results
            results_map: Dict[str, Any] = {}
            async for item in await client.messages.batches.results(batch.id):
                results_map[item.custom_id] = item

            for job in jobs:
                item = results_map.get(job.job_id)
                if item and item.result.type == "succeeded":
                    msg = item.result.message
                    text = msg.content[0].text if msg.content else ""
                    u = msg.usage
                    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
                    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
                    effective_input = int(
                        u.input_tokens + cache_read * 0.1 + cache_write * 1.25
                    )
                    cost = claude_client.calculate_cost(effective_input, u.output_tokens)
                    result = {
                        "response": text,
                        "model": claude_client.config.name,
                        "input_tokens": effective_input,
                        "output_tokens": u.output_tokens,
                        "cost_usd": cost,
                        "latency_ms": 0,
                        "cache_hit": False,
                        "batched": True,
                    }
                    if not job.future.done():
                        job.future.set_result(result)
                else:
                    error_msg = f"Batch result missing or errored for job {job.job_id}"
                    logger.error(error_msg)
                    if not job.future.done():
                        job.future.set_exception(RuntimeError(error_msg))

        except Exception as exc:
            logger.error(f"Anthropic batch API failed: {exc} — falling back to sequential")
            for job in jobs:
                await self._run_job(job)
