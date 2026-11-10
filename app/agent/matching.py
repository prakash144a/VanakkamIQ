"""Fuzzy matching tuned for speech: callers say "chicken biriyani" or "the dosa", not exact names."""

import re
from difflib import SequenceMatcher

# Filler words callers say around names ("can I get the dosa please").
STOPWORDS = {
    "a", "an", "the", "some", "please", "of", "and", "with", "can", "could", "get", "have", "want",
    "like", "order", "one", "two", "three", "four", "five", "my", "your", "i", "id", "me",
}


def _norm(text: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split()
    kept = [w for w in words if w not in STOPWORDS]
    return " ".join(kept or words)


def _tokens(text: str) -> set[str]:
    return {t for t in _norm(text).split() if len(t) > 1}


def text_score(query: str, candidate: str) -> float:
    q, c = _norm(query), _norm(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    ratio = SequenceMatcher(None, q, c).ratio()
    q_tokens, c_tokens = _tokens(q), _tokens(c)
    overlap = len(q_tokens & c_tokens) / len(q_tokens) if q_tokens else 0.0
    # fuzzy token overlap handles "biriyani" vs "biryani"
    fuzzy = 0.0
    if q_tokens and c_tokens:
        fuzzy = sum(max(SequenceMatcher(None, qt, ct).ratio() for ct in c_tokens) for qt in q_tokens) / len(q_tokens)
    contains = 0.9 if (q in c or c in q) else 0.0
    return max(ratio, overlap, fuzzy * 0.95, contains)


def search_faqs(faqs: list, question: str, limit: int = 2, min_score: float = 0.5) -> list:
    scored = [(f, text_score(question, f.question)) for f in faqs]
    scored = [pair for pair in scored if pair[1] >= min_score]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [f for f, _ in scored[:limit]]
