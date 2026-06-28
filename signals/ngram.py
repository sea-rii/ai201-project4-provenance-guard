"""
signals/ngram.py - Signal 3: N-gram repetition rate (perplexity proxy).

AI models are penalised for repetition during RLHF training, producing
a characteristic low-repetition-rate signature. Human writing naturally
repeats bigrams and phrases.

Output contract (planning.md §7 SF-1):
    0.0 = high repetition -> human-like
    1.0 = low repetition  -> AI-like

Requires 100+ tokens for a reliable signal; returns 0.5 (abstain) otherwise.
"""

import re
from collections import Counter


def _tokenise(text):
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return [w for w in text.split() if w]


def _bigrams(tokens):
    return [(tokens[i], tokens[i + 1]) for i in range(len(tokens) - 1)]


def ngram_score(text):
    """
    Compute n-gram repetition AI-probability score.

    Returns float in [0, 1]:
        0.0 = lots of repeated bigrams -> human-like
        1.0 = low repetition -> AI-like
        0.5 = abstain (text too short, < 100 tokens)
    """
    if not text or not text.strip():
        return 0.5

    tokens = _tokenise(text)

    if len(tokens) < 100:
        return 0.5  # insufficient sample for reliable bigram statistics

    grams = _bigrams(tokens)
    if not grams:
        return 0.5

    counts = Counter(grams)
    total = len(grams)
    repeated = sum(1 for g, c in counts.items() if c > 1)
    repetition_rate = repeated / total

    # repetition_rate near 0 -> AI-like (score near 0.65, not 1.0 - short texts
    # can hit zero repetition just due to diversity, so we cap the ceiling)
    # repetition_rate >= 0.4 -> clearly human-like (score 0.0)
    score = 1.0 - min(repetition_rate / 0.4, 1.0)
    score = max(0.0, min(0.80, score))  # cap: zero-repetition => 0.80, not 1.0
    return round(score, 4)


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
            "said it was better. probably wont go back unless someone drags me there"
        )),
    ]
    print(f"\n{'Sample':<25} {'Tokens':>8} {'N-gram Score':>14}")
    print("-" * 50)
    for label, text in samples:
        tokens = _tokenise(text)
        score = ngram_score(text)
        print(f"{label:<25} {len(tokens):>8} {score:>14.4f}")