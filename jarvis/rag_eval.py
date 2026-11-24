"""Does the study search actually find the right page? A self-check on the user's own material.

For a sample of stored passages, build a question-like query from words that are distinctive to
that passage (as a student would: the key terms, not the exact sentence) and see whether search
brings that same passage back. The share found in the top 1 and top 5 is a measure of search
quality that can be tracked as the notes grow or the search is changed.
"""
from __future__ import annotations

import random
import re
from collections import Counter

from .store import STOPWORDS, Store


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9]{3,}", text.lower()) if w not in STOPWORDS]


def evaluate(store: Store, samples: int = 40, seed: int = 7, terms: int = 5, skip_notes: bool = True) -> dict:
    rows = store._rows("SELECT rowid AS id, source, passage, page FROM knowledge")
    rows = [r for r in rows if len(r["passage"]) > 200 and not (skip_notes and r["source"].startswith("note:"))]
    if not rows:
        return {"samples": 0, "top1": None, "top5": None, "misses": []}
    document_frequency = Counter(w for r in rows for w in set(_words(r["passage"])))
    random.Random(seed).shuffle(rows)
    hits1 = hits5 = tested = 0
    misses = []
    for r in rows[:samples]:
        own = Counter(_words(r["passage"]))
        # distinctive = frequent here, rare elsewhere
        ranked = sorted(own, key=lambda w: (own[w] / document_frequency[w], len(w)), reverse=True)[:terms]
        if len(ranked) < 2:
            continue
        found = [h["passage"] for h in store.search_knowledge(" ".join(ranked), limit=5)]
        tested += 1
        if found and found[0] == r["passage"]:
            hits1 += 1
        if r["passage"] in found:
            hits5 += 1
        else:
            misses.append(f"{r['source']} {r['page']}: {' '.join(ranked)}")
    return {"samples": tested, "top1": round(100 * hits1 / tested) if tested else None,
            "top5": round(100 * hits5 / tested) if tested else None, "misses": misses[:5]}


def describe(result: dict) -> str:
    if not result["samples"]:
        return "Nothing to test yet: teach me some notes first (learn_document or learn_folder)."
    text = (f"Search check on {result['samples']} passages from your notes: the right page came first "
            f"{result['top1']}% of the time and in the top five {result['top5']}%.")
    if result["misses"]:
        text += " Missed: " + "; ".join(result["misses"][:3])
    return text
