"""PII redaction applied at ingest and again on every answer.

Defense in depth: ingest redaction keeps PII out of the index, SQL mirror and
embeddings; output redaction covers legacy rows and generated text.
"""
from __future__ import annotations

import re

# kn: regex PII detector; misses names/addresses and context-dependent PII — swap for Presidio when real corpora arrive
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_CARD = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
_PHONE = re.compile(r"(?<![\w.-])(?:\+?1[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}(?!\d)")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group())
    return "[REDACTED:CARD]" if _luhn(digits) else m.group()


def redact(text: str) -> str:
    text = _EMAIL.sub("[REDACTED:EMAIL]", text)
    text = _SSN.sub("[REDACTED:SSN]", text)
    text = _CARD.sub(_card, text)
    return _PHONE.sub("[REDACTED:PHONE]", text)
