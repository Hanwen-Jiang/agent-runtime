"""Optional lexical ordering of already admitted, nonredundant memory candidates.

IDF and length statistics use only the provided candidates. Equal scores retain
the preceding retrieval order. This module never receives benchmark labels.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence


def bm25_order(query: Sequence[str], documents: Sequence[Sequence[str]]) -> list[int]:
    """Return stable BM25 order with fixed k1=1.2 and b=0.75."""
    counts = [Counter(document) for document in documents]
    n = len(counts)
    lengths = [sum(document.values()) for document in counts]
    if not n or not query or not sum(lengths):
        return list(range(n))
    df = Counter(token for document in counts for token in document)
    average = sum(lengths) / n
    query_counts = Counter(query)
    scores = []
    for document, length in zip(counts, lengths, strict=True):
        score = 0.0
        for token, frequency in query_counts.items():
            tf = document[token]
            if tf:
                idf = math.log(1 + (n - df[token] + 0.5) / (df[token] + 0.5))
                score += frequency * idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / average))
        scores.append(score)
    return sorted(range(n), key=lambda index: (-scores[index], index))
