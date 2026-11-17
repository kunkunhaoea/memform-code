"""Bounded, local evidence retrieval. No labels, API calls or text rewriting.

This scans the omitted pool locally; only the bounded shortlist reaches the LLM.
The character limit includes JSON record wrappers, not just observation text.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter

from .interface import Evidence


def terms(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.casefold())
    return words + [a + b for a, b in zip(words, words[1:])
                    if re.fullmatch(r"[\u4e00-\u9fff]", a)
                    and re.fullmatch(r"[\u4e00-\u9fff]", b)]


def shortlist(omitted: tuple[Evidence, ...], query: str, *, max_records: int,
              max_chars: int) -> tuple[tuple[Evidence, ...], dict]:
    """BM25-style ranking, with deterministic ties and whole-record packing."""
    query_terms = sorted(set(terms(query)))
    lengths, counts, frequencies = [], [], Counter()
    for record in omitted:
        count = Counter(terms(record.text))
        lengths.append(sum(count.values()))
        # Retain only query postings; avoid an in-memory copy of all pool tokens.
        relevant = {term: count[term] for term in query_terms if count[term]}
        counts.append(relevant)
        frequencies.update(relevant.keys())
    average = sum(lengths) / max(1, len(lengths)) or 1
    scores = []
    for record, count, length in zip(omitted, counts, lengths):
        score = 0.0
        for term, tf in count.items():
            idf = math.log(1 + (len(omitted) - frequencies[term] + .5) /
                           (frequencies[term] + .5))
            score += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * length / average))
        scores.append((score, record))
    ranked = sorted(scores, key=lambda item: (-round(item[0], 12), item[1].record_id))
    chosen: list[Evidence] = []
    used = 2  # surrounding JSON array
    oversized = 0
    for _, record in ranked:
        size = len(json.dumps({"record_id": record.record_id, "text": record.text},
                              ensure_ascii=False)) + (2 if chosen else 0)
        if size + 2 > max_chars:
            oversized += 1
            continue
        if len(chosen) < max_records and used + size <= max_chars:
            chosen.append(record)
            used += size
    return tuple(chosen), {
        "retrieval_strategy": "local_bm25", "local_records_scanned": len(omitted),
        "retrieval_shortlist_count": len(chosen), "retrieval_payload_chars": used,
        "retrieval_max_records": max_records, "retrieval_max_chars": max_chars,
        "oversized_records_skipped": oversized,
        "retrieval_excluded_count": len(omitted) - len(chosen),
    }
