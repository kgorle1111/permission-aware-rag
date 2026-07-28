"""One structured LLM call over permission-filtered context, with prompt caching.

Cache design: the big static block (system prompt = underwriting guidelines) gets
cache_control so repeated queries reuse it; per-request retrieved context goes in
the user message, uncached, because it changes every call. Usage stats are
returned so cache hits (cache_read_input_tokens) are visible per response.

No API key set -> ask() returns None and callers fall back to retrieval-only.
Stdlib urllib only — no anthropic SDK dependency.
"""
import json
import os
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5"  # ponytail: smallest tier; grounded Q&A over supplied context needs no bigger model

# Static guidelines block — the cacheable prefix. Caching engages once this
# exceeds the model's minimum cacheable size (2048 tokens on Haiku); grow it
# with the real underwriting manual and cache reads show up in usage.
SYSTEM_PROMPT = """You are an internal underwriting research assistant. You answer questions
for underwriters using ONLY the document excerpts supplied in the user message. Those
excerpts have already been filtered by the caller's access permissions — never speculate
about documents that are not present.

Rules:
1. Answer only from the supplied context. If the context does not contain the answer,
   say exactly: "The documents you have access to do not answer this."
2. Cite the document id (e.g. [claims-2024]) after every factual claim.
3. Never infer or guess policy status, claim amounts, balances, or credit decisions.
4. Flag conflicts: if two excerpts disagree, state both with citations.
5. You draft; the underwriter decides. Never phrase output as an approval or denial —
   phrase it as findings for human review.
6. Keep answers under 200 words, findings first.
"""


def ask(question, chunks, timeout=60):
    """Return {"answer", "usage"} or None if no ANTHROPIC_API_KEY is set."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    context = "\n\n".join(f"[{c['doc_id']}] {c['text']}" for c in chunks) or "(no accessible documents matched)"
    body = {
        "model": MODEL,
        "max_tokens": 600,
        "system": [{"type": "text", "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user",
                      "content": f"Context (permission-filtered):\n{context}\n\nQuestion: {question}"}],
    }
    req = urllib.request.Request(
        API_URL, json.dumps(body).encode(),
        {"content-type": "application/json", "x-api-key": key,
         "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return {"answer": data["content"][0]["text"], "usage": data.get("usage", {})}
