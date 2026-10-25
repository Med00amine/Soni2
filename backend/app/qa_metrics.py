"""Word and character error rates between what should be said and what was heard.

Both texts are first reduced to Spanish canonical tokens
(:mod:`app.spanish_normalization`), so "147" and "ciento cuarenta y siete"
count as one matching token, accents and punctuation do not count, and a wrong
quantity still counts as a substitution. The alignment is a true Levenshtein
alignment, so substitutions, deletions and insertions are counted exactly
rather than estimated from a similarity ratio.

These numbers measure agreement with a recognizer, not pronunciation quality:
an ASR transcript that matches proves the words are recognizable, not that
they sound right.
"""

from __future__ import annotations

from app.spanish_normalization import canonical_tokens


def _levenshtein_ops(reference: list[str], hypothesis: list[str]) -> tuple[int, int, int, int]:
    """(substitutions, deletions, insertions, matches) of a minimal alignment."""
    rows, cols = len(reference), len(hypothesis)
    # cost, substitutions, deletions, insertions per cell; two rows at a time.
    previous = [(j, 0, 0, j) for j in range(cols + 1)]
    for i in range(1, rows + 1):
        current = [(i, 0, i, 0)]
        for j in range(1, cols + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                diagonal = previous[j - 1]
                best = diagonal
            else:
                cost, s, d, n = previous[j - 1]
                best = (cost + 1, s + 1, d, n)
            cost, s, d, n = previous[j]
            if cost + 1 < best[0]:
                best = (cost + 1, s, d + 1, n)
            cost, s, d, n = current[j - 1]
            if cost + 1 < best[0]:
                best = (cost + 1, s, d, n + 1)
            current.append(best)
        previous = current
    _cost, substitutions, deletions, insertions = previous[cols]
    matches = rows - substitutions - deletions
    return substitutions, deletions, insertions, matches


def _chars(tokens: list[str]) -> list[str]:
    return list(" ".join(tokens))


def error_rates(expected: str, recognized: str, *, with_cer: bool = True, **options) -> dict:
    """WER/CER and the S/D/I breakdown of ``recognized`` against ``expected``."""
    reference_tokens, hypothesis_tokens = canonical_tokens(expected, recognized, **options)
    reference = [token.value for token in reference_tokens]
    hypothesis = [token.value for token in hypothesis_tokens]
    substitutions, deletions, insertions, matches = _levenshtein_ops(reference, hypothesis)
    count = len(reference)
    result = {
        "reference_words": count,
        "recognized_words": len(hypothesis),
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "matches": matches,
        "wer": round((substitutions + deletions + insertions) / count, 4) if count else (0.0 if not hypothesis else 1.0),
    }
    if with_cer:
        left, right = _chars(reference), _chars(hypothesis)
        if len(left) * max(1, len(right)) <= 4_000_000:
            s, d, i, _m = _levenshtein_ops(left, right)
            result["cer"] = round((s + d + i) / len(left), 4) if left else 0.0
        else:
            result["cer"] = None
    return result


def aggregate(rows: list[dict]) -> dict:
    """Corpus-level rates: errors summed over words, not averaged per segment."""
    totals = {key: sum(int(row.get(key) or 0) for row in rows)
              for key in ("reference_words", "recognized_words", "substitutions", "deletions", "insertions")}
    words = totals["reference_words"]
    errors = totals["substitutions"] + totals["deletions"] + totals["insertions"]
    totals["wer"] = round(errors / words, 4) if words else None
    totals["segments"] = len(rows)
    totals["segments_with_errors"] = sum(1 for row in rows
                                         if (row.get("substitutions") or row.get("deletions") or row.get("insertions")))
    return totals
