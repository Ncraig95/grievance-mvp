from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass


_DEFAULT_MAX_CHARS = 140
_CACHE_LIMIT = 512
_SUMMARY_CACHE: dict[tuple[str, int], str] = {}

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_DATE_RE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2})\b",
    flags=re.IGNORECASE,
)
_SPACE_RE = re.compile(r"\s+")

_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "her",
    "his",
    "i",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "our",
    "she",
    "that",
    "the",
    "their",
    "they",
    "this",
    "to",
    "was",
    "were",
    "will",
    "with",
}
_KEYWORDS = {
    "article",
    "attendance",
    "contract",
    "denied",
    "discipline",
    "discharge",
    "grievance",
    "meeting",
    "overtime",
    "pay",
    "request",
    "seniority",
    "settlement",
    "suspension",
    "termination",
    "violation",
}
_BOILERPLATE_TEXT = {
    "auto data request generated from officer panel",
    "data request cover letter generated from intake",
    "data request generated from intake",
    "disciplinary grievance brief",
    "non-discipline grievance brief",
    "non discipline grievance brief",
    "true intent grievance brief",
}
_PRIMARY_NARRATIVE_FIELDS = (
    "statement_full_text",
    "statement_text",
    "q3_union_statement",
    "union_statement",
    "issue_text",
    "issue_or_condition_involved",
    "issue_involved",
    "chronology_of_facts",
    "analysis_of_grievance",
    "analysis",
    "union_facts",
    "union_argument",
    "company_facts",
    "company_argument",
    "q4_contract_basis",
    "issue_summary",
    "issue_contract_section",
    "narrative",
)
_LOW_PRIORITY_NARRATIVE_FIELDS = (
    "action_taken",
    "current_status",
)


@dataclass(frozen=True)
class GrievanceSummary:
    summary: str | None
    full_text: str | None
    source: str | None


def build_grievance_summary(
    payload: dict[str, object],
    *,
    manual_text: object | None = None,
    max_chars: int = _DEFAULT_MAX_CHARS,
    include_low_priority: bool = True,
) -> GrievanceSummary:
    """Build a short, local-only overview summary from known grievance text fields."""
    candidates = _candidate_texts(payload, manual_text=manual_text, include_low_priority=include_low_priority)
    if not candidates:
        return GrievanceSummary(summary=None, full_text=None, source=None)

    useful = [(source, text) for source, text in candidates if not _is_boilerplate(text)]
    source, full_text = useful[0] if useful else candidates[0]
    return GrievanceSummary(
        summary=summarize_text(full_text, max_chars=max_chars),
        full_text=full_text,
        source=source,
    )


def summarize_text(text: object, *, max_chars: int = _DEFAULT_MAX_CHARS) -> str | None:
    normalized = _normalize_text(text)
    if not normalized:
        return None
    if len(normalized) <= max_chars:
        return normalized

    cache_key = (_hash_text(normalized), int(max_chars))
    cached = _SUMMARY_CACHE.get(cache_key)
    if cached:
        return cached

    sentences = _split_sentences(normalized)
    if not sentences:
        summary = _truncate_words(normalized, max_chars=max_chars)
    else:
        summary = _summarize_sentences(sentences, max_chars=max_chars)

    if len(_SUMMARY_CACHE) >= _CACHE_LIMIT:
        _SUMMARY_CACHE.pop(next(iter(_SUMMARY_CACHE)))
    _SUMMARY_CACHE[cache_key] = summary
    return summary


def _candidate_texts(
    payload: dict[str, object],
    *,
    manual_text: object | None,
    include_low_priority: bool,
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    _add_field_candidates(candidates, seen, payload, _PRIMARY_NARRATIVE_FIELDS)
    if include_low_priority:
        _add_field_candidates(candidates, seen, payload, _LOW_PRIORITY_NARRATIVE_FIELDS)
    _add_candidate(candidates, seen, "tracking_issue_summary", manual_text)

    return candidates


def low_priority_grievance_text_candidates(payload: dict[str, object]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    _add_field_candidates(candidates, seen, payload, _LOW_PRIORITY_NARRATIVE_FIELDS)
    return candidates


def is_low_priority_grievance_text(payload: dict[str, object], value: object) -> bool:
    text = _normalize_text(value)
    if not text:
        return False
    return any(text == candidate_text for _source, candidate_text in low_priority_grievance_text_candidates(payload))


def _add_field_candidates(
    candidates: list[tuple[str, str]],
    seen: set[str],
    payload: dict[str, object],
    fields: tuple[str, ...],
) -> None:
    template_data = payload.get("template_data")
    template_payload = template_data if isinstance(template_data, dict) else {}
    for field in fields:
        _add_candidate(candidates, seen, f"template_data.{field}", template_payload.get(field))
        _add_candidate(candidates, seen, field, payload.get(field))


def _add_candidate(
    candidates: list[tuple[str, str]],
    seen: set[str],
    source: str,
    value: object,
) -> None:
    text = _normalize_text(value)
    if not text:
        return
    key = text.lower()
    if key in seen:
        return
    seen.add(key)
    candidates.append((source, text))


def _summarize_sentences(sentences: list[str], *, max_chars: int) -> str:
    tokens = [
        token.lower()
        for sentence in sentences
        for token in _WORD_RE.findall(sentence)
        if token.lower() not in _STOP_WORDS and len(token) > 2
    ]
    frequency = Counter(tokens)
    scored = sorted(
        ((_sentence_score(sentence, idx, frequency), idx, sentence) for idx, sentence in enumerate(sentences)),
        key=lambda item: (-item[0], item[1]),
    )

    selected: list[tuple[int, str]] = []
    for _score, idx, sentence in scored:
        if not selected:
            selected.append((idx, sentence))
            continue
        candidate = " ".join(text for _idx, text in sorted([*selected, (idx, sentence)]))
        if len(candidate) <= max_chars and len(selected) < 2:
            selected.append((idx, sentence))
        if len(selected) >= 2:
            break

    summary = " ".join(text for _idx, text in sorted(selected))
    return _truncate_words(summary, max_chars=max_chars)


def _sentence_score(sentence: str, idx: int, frequency: Counter[str]) -> float:
    words = [
        token.lower()
        for token in _WORD_RE.findall(sentence)
        if token.lower() not in _STOP_WORDS and len(token) > 2
    ]
    if not words:
        return 0.0
    score = sum(frequency[word] for word in words) / max(len(words), 1)
    if idx == 0:
        score += 2.0
    if _DATE_RE.search(sentence):
        score += 3.0
    if any(word in _KEYWORDS for word in words):
        score += 2.0
    if re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", sentence):
        score += 1.0
    return score


def _split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_RE.split(text) if part.strip()]


def _truncate_words(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "..."
    limit = max(max_chars - len(suffix), 1)
    clipped = text[:limit].rsplit(" ", 1)[0].strip()
    if not clipped:
        clipped = text[:limit].strip()
    return f"{clipped.rstrip('.,;:')}..."


def _normalize_text(value: object) -> str | None:
    text = _SPACE_RE.sub(" ", str(value or "").replace("\x00", " ")).strip()
    return text or None


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_boilerplate(text: str) -> bool:
    normalized = _SPACE_RE.sub(" ", text.strip().lower()).strip(" .")
    return normalized in _BOILERPLATE_TEXT
