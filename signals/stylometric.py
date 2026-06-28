"""
signals/stylometric.py — Signal 2: Stylometric heuristics.

Measures three statistically computable properties of text that differ
between human and AI writing. Returns a single float in [0, 1].

Output contract (planning.md §2, §8):
    0.0 = statistically human-like (high variance, low TTR, irregular punctuation)
    1.0 = statistically AI-like   (low variance, high TTR, uniform punctuation)

Sub-measures:
    1. Sentence-length variance  — AI text is more uniform → low variance → high score
    2. Type-token ratio (TTR)    — AI text avoids repetition → high TTR → high score
    3. Punctuation density       — humans use dashes/ellipses more irregularly

Edge case (planning.md §11 edge case 1):
    Returns (score, low_sample=True) when word_count < 80.
    Caller can choose to down-weight this signal.
"""

import re
import math
import string


# ── Sentence tokeniser ────────────────────────────────────────────────────────

def _split_sentences(text: str) -> list[str]:
    """Split on . ! ? followed by whitespace or end-of-string."""
    raw = re.split(r"(?<=[.!?])\s+", text.strip())
    return [s for s in raw if s.strip()]


def _word_count(text: str) -> int:
    return len(text.split())


# ── Sub-measure 1: sentence-length variance ───────────────────────────────────

def _sentence_length_variance_score(sentences: list[str]) -> float:
    """
    Low variance → AI-like → score near 1.
    High variance → human-like → score near 0.

    Empirical calibration:
        std < 3  words  → very uniform  → score 0.90
        std > 15 words  → very variable → score 0.05
    We map std dev to [0,1] using a sigmoid-like clamp.
    """
    if len(sentences) < 2:
        return 0.5  # can't compute variance from one sentence

    lengths = [len(s.split()) for s in sentences]
    mean = sum(lengths) / len(lengths)
    variance = sum((l - mean) ** 2 for l in lengths) / len(lengths)
    std = math.sqrt(variance)

    # Clamp: std=0 → 1.0 (pure AI), std=20+ → 0.0 (very human)
    # Linear interpolation then clamp
    score = 1.0 - min(std / 20.0, 1.0)
    return round(score, 4)


# ── Sub-measure 2: type-token ratio ──────────────────────────────────────────

def _ttr_score(text: str) -> float:
    """
    TTR = unique_words / total_words.

    AI text has suspiciously HIGH TTR (avoids repetition).
    Human text, especially conversational, repeats words naturally.

    BUT: TTR is length-dependent — longer texts always score lower.
    We use a corrected TTR: measure on the first 100 tokens only (RTTR proxy).

    High TTR (> 0.80) → AI-like → score near 1.
    Low TTR  (< 0.40) → human-like → score near 0.

    Calibration:
        ttr >= 0.85 → 0.95
        ttr <= 0.40 → 0.05
    """
    words = [w.lower().strip(string.punctuation) for w in text.split()]
    words = [w for w in words if w]

    # Use first 100 words to normalise for length
    sample = words[:100]
    if len(sample) < 5:
        return 0.5

    ttr = len(set(sample)) / len(sample)

    # Map [0.40, 0.85] → [0.0, 1.0], clamp outside
    lo, hi = 0.55, 0.92  # recalibrated: short human text TTR is naturally high
    score = (ttr - lo) / (hi - lo)
    score = max(0.0, min(1.0, score))
    return round(score, 4)


# ── Sub-measure 3: punctuation density / irregularity ────────────────────────

def _punctuation_score(text: str) -> float:
    """
    Measures the ratio of 'expressive' punctuation (dashes, ellipses,
    exclamation marks, question marks) to total words.

    Human writers use these irregularly and more frequently.
    AI writers produce clean, comma-and-period prose.

    High expressive punctuation → human-like → score near 0.
    Low expressive punctuation  → AI-like    → score near 1.

    Calibration (per 100 words):
        rate >= 8  → very human → 0.05
        rate <= 1  → very AI    → 0.95
    """
    words = text.split()
    if not words:
        return 0.5

    expressive = re.findall(r"[—–\-]{1,2}|\.{2,}|[!?]", text)
    rate_per_100 = (len(expressive) / len(words)) * 100

    # Map [1, 8] → [1.0, 0.0] (inverted: more expressive = more human = lower score)
    lo, hi = 0.0, 8.0  # 0 expressive punct is common even in human text
    score = 1.0 - (rate_per_100 - lo) / (hi - lo)
    score = max(0.0, min(1.0, score))
    return round(score, 4)


# ── Combined stylometric score ────────────────────────────────────────────────

# Sub-measure weights (sum to 1.0)
_SUB_WEIGHTS = {
    "variance":    0.45,
    "ttr":         0.35,
    "punctuation": 0.20,
}


def stylometric_score(text: str) -> tuple[float, bool]:
    """
    Compute combined stylometric AI-probability score.

    Returns:
        (score: float [0,1], low_sample: bool)

    low_sample=True when word_count < 80 — caller should down-weight
    this signal per planning.md §11 edge case 1.
    """
    if not text or not text.strip():
        return 0.5, True

    words = text.split()
    word_count = len(words)
    low_sample = word_count < 80

    sentences = _split_sentences(text)

    var_score   = _sentence_length_variance_score(sentences)
    ttr_s       = _ttr_score(text)
    punct_score = _punctuation_score(text)

    combined = (
        _SUB_WEIGHTS["variance"]    * var_score
        + _SUB_WEIGHTS["ttr"]         * ttr_s
        + _SUB_WEIGHTS["punctuation"] * punct_score
    )

    return round(combined, 4), low_sample


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    samples = [
        ("CLEARLY AI", (
            "Artificial intelligence represents a transformative paradigm shift in modern "
            "society. It is important to note that while the benefits of AI are numerous, "
            "it is equally essential to consider the ethical implications. Furthermore, "
            "stakeholders across various sectors must collaborate to ensure responsible "
            "deployment of these technologies in a manner that is both inclusive and equitable."
        )),
        ("CLEARLY HUMAN", (
            "ok so i finally tried that new ramen place downtown and honestly? "
            "underwhelming. the broth was fine but they put WAY too much sodium in it and "
            "i was thirsty for like three hours after. my friend got the spicy version and "
            "said it was better. probably won't go back unless someone drags me there lol"
        )),
        ("BORDERLINE FORMAL HUMAN", (
            "The relationship between monetary policy and asset price inflation has been "
            "extensively studied in the literature. Central banks face a fundamental tension "
            "between their mandate for price stability and the unintended consequences of "
            "prolonged low interest rates on equity and real estate valuations."
        )),
        ("BORDERLINE EDITED AI", (
            "I've been thinking a lot about remote work lately. There are genuine tradeoffs — "
            "flexibility and no commute on one side, isolation and blurred work-life boundaries "
            "on the other. Studies show productivity varies widely by individual and role type."
        )),
    ]

    print(f"\n{'Sample':<25} {'Variance':>10} {'TTR':>8} {'Punct':>8} {'Combined':>10} {'LowSample':>10}")
    print("-" * 75)
    for label, text in samples:
        sentences = _split_sentences(text)
        v = _sentence_length_variance_score(sentences)
        t = _ttr_score(text)
        p = _punctuation_score(text)
        score, low = stylometric_score(text)
        print(f"{label:<25} {v:>10.4f} {t:>8.4f} {p:>8.4f} {score:>10.4f} {str(low):>10}")