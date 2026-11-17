"""Locate observation quotes without fuzzy matching or changing record contents."""
from __future__ import annotations

import copy
import re
from typing import Any


def observation_quote(text: str, quote: Any) -> str | None:
    """Exact match, or a unique sentence-initial capitalization correction.

    Only the first letter of an ordinary word may differ. Identifiers, acronyms,
    interior capitalization, whitespace and punctuation are never normalized.
    Policy/edit spans remain strictly exact and do not use this function.
    """
    if not isinstance(quote, str) or len(quote.strip()) < 12:
        return None
    if quote in text:
        return quote
    if not re.match(r"[A-Za-z][a-z]+\b", quote):
        return None
    corrected = quote[0].swapcase() + quote[1:]
    start = text.find(corrected)
    if start < 0 or start != text.rfind(corrected):
        return None
    prefix = text[:start].rstrip(" \t\r")
    if prefix and prefix[-1] not in ".!?:\n":
        return None
    return corrected


def canonicalize_citations(value: dict[str, Any], evidence: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Keep raw replies intact; log every correction in the parsed view."""
    parsed = copy.deepcopy(value)
    corrections = []

    def visit(item: Any, path: str) -> None:
        if isinstance(item, dict):
            rid, quote = item.get("record_id"), item.get("quote")
            if isinstance(rid, str) and rid in evidence:
                resolved = observation_quote(evidence[rid], quote)
                if resolved is not None and resolved != quote:
                    item["quote"] = resolved
                    corrections.append({"path": path, "record_id": rid,
                                        "original_quote": quote, "canonical_quote": resolved})
            for key, child in item.items():
                visit(child, path + "." + key)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")

    visit(parsed, "reply")
    return parsed, corrections
