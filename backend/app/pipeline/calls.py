"""Model-call bookkeeping shared by pipeline stages (reader, SAM discovery, re-look)."""

from ..llm import CallMeta


async def record_call(pool, meta: CallMeta) -> None:
    await pool.execute(
        """INSERT INTO model_calls (call_id, provider, model, purpose, prompt_version, latency_ms, input_tokens,
                                    output_tokens, cache_hit, ok, error)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) ON CONFLICT (call_id) DO NOTHING""",
        meta.call_id, meta.provider, meta.model, meta.purpose, meta.prompt_version, meta.latency_ms,
        meta.input_tokens, meta.output_tokens, meta.cache_hit, meta.ok, meta.error)
