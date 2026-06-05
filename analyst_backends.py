"""
analyst_backends.py — pluggable AI tiebreaker backends.

NuroQ's AI is a gated ~10-point tiebreaker; the deterministic quant rubric is the
real signal. All inference is routed through `EnsembleAnalyst.analyze()` in
dashboard.py — historically MLX-Gemma (Apple-Silicon only). For cloud (GCP/Linux),
that single chokepoint delegates to one of these backends instead, selected by the
`NUROQ_AI_BACKEND` env var:

    gemma   → local MLX Gemma           (Mac default; handled inline in dashboard)
    gemini  → Google Gemini API         (cloud default; this module)

Each backend exposes the same contract:

    backend.generate(prompt: str) -> str     # raw model text; parsed downstream
    backend.describe() -> str                 # short label for logs

Adding Anthropic/Modal later = one more class + a branch in make_backend().
The heavy SDK import is done lazily inside __init__ so importing this module is
free and never drags in a vendor SDK that isn't being used.
"""

from __future__ import annotations

import os
import threading

# JSON schema for the SCORING path. Forcing structured output guarantees Gemini
# returns complete, parseable JSON with a `score` (its verbose free-text replies
# were truncating the JSON before the score key, so get_structured_data defaulted
# to 50). NOT applied to the Ask-AI path, which wants free-form prose.
_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "score":          {"type": "integer"},
        "rating":         {"type": "string", "enum": ["BUY", "HOLD", "SELL"]},
        "reasoning":      {"type": "string"},
        "considerations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "rating", "reasoning"],
}


class GeminiBackend:
    """Google Gemini via the unified `google-genai` SDK.

    Auth, two modes (pick with NUROQ_GEMINI_VERTEX):
      • API key  (default) — set GEMINI_API_KEY (or GOOGLE_API_KEY). Simplest;
        works with an AI Studio key anywhere.
      • Vertex AI (NUROQ_GEMINI_VERTEX=1) — uses Application Default Credentials
        (the GCE VM's service account). No key to manage. Needs
        GOOGLE_CLOUD_PROJECT (+ GOOGLE_CLOUD_LOCATION, default us-central1).

    A small semaphore bounds concurrency (replaces the MLX/Metal `_gemma_lock` —
    there is no GPU command buffer to serialize here, but we still cap fan-out so
    a burst of notable-event triggers can't hammer the API).
    """

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 max_concurrency: int = 4):
        from google import genai  # lazy: only when the gemini backend is used

        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.max_output_tokens = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "700"))
        use_vertex = os.getenv("NUROQ_GEMINI_VERTEX", "0") == "1"

        if use_vertex:
            project = os.getenv("GOOGLE_CLOUD_PROJECT")
            if not project:
                raise RuntimeError(
                    "NUROQ_GEMINI_VERTEX=1 but GOOGLE_CLOUD_PROJECT is not set."
                )
            self.client = genai.Client(
                vertexai=True,
                project=project,
                location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            )
            self._auth = f"vertex:{project}"
        else:
            key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if not key:
                raise RuntimeError(
                    "GEMINI_API_KEY (or GOOGLE_API_KEY) not set, and "
                    "NUROQ_GEMINI_VERTEX != 1. Set one to use the gemini backend."
                )
            self.client = genai.Client(api_key=key)
            self._auth = "api_key"

        self._sem = threading.Semaphore(max(1, max_concurrency))

    def describe(self) -> str:
        return f"{self.model} ({self._auth})"

    def generate(self, prompt: str, structured: bool = False) -> str:
        """Generate text. `structured=True` (the scoring path) constrains output
        to the analysis JSON schema so the score/rating always parse; the Ask-AI
        path leaves it False for free-form prose."""
        from google.genai import types

        cfg_kwargs = dict(
            temperature=0.0,
            # Scoring JSON needs headroom so a long reasoning field doesn't push
            # the score key past the limit; free-text answers also get a bump.
            max_output_tokens=2048 if structured else max(self.max_output_tokens, 1024),
        )
        if structured:
            cfg_kwargs["response_mime_type"] = "application/json"
            cfg_kwargs["response_schema"] = _ANALYSIS_SCHEMA

        with self._sem:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
        # `.text` concatenates the candidate's text parts; guard against None.
        return getattr(resp, "text", None) or ""


def make_backend(name: str):
    """Factory: map NUROQ_AI_BACKEND → a backend instance. Raises on unknown."""
    key = (name or "").strip().lower()
    if key in ("gemini", "vertex", "google"):
        return GeminiBackend()
    raise ValueError(
        f"Unknown NUROQ_AI_BACKEND={name!r}. Supported: 'gemma' (local MLX, "
        f"handled in dashboard) or 'gemini'."
    )
