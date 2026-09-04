"""Title feature extraction and pattern analysis.

Deliberately split in two.

**Deterministic features** — length, numbers, brackets, capitalisation, emoji,
opening phrase — are computed locally, cost nothing, and exist for every video
the app has ever seen. They are also the only title metrics that are actually
*facts*.

**Model-scored features** — curiosity, rhetorical angle — cost money and are
judgements. They are stored in nullable columns and always displayed as
opinions, never blended into a single "title score out of 100". A composite
score would be the thing this app exists to avoid: a number that looks
authoritative, can't be argued with, and tells you nothing about what to change.

What makes this useful is the comparison, not the features themselves. Knowing
your titles average 58 characters is trivia; knowing the outliers in your niche
average 41 while yours average 58 is a decision.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import TitleAnalysis, Video

log = logging.getLogger(__name__)

#: Titles get clipped around here on the homepage and in the sidebar. The exact
#: cutoff varies by surface and viewport, so this is a risk flag, not a rule.
TRUNCATION_CHARS = 60

#: Words that consistently show up in high-performing titles across niches.
#: This is a lexicon for *describing* a title, not a scoring system — the app
#: reports which ones appear, and lets the outlier comparison say whether they
#: correlate with anything on your channel.
POWER_WORDS = frozenset(
    {
        "actually", "banned", "before", "best", "biggest", "brutal", "cheap",
        "compared", "crazy", "dangerous", "deleted", "destroyed", "easy",
        "everything", "exposed", "extreme", "fastest", "finally", "first",
        "forever", "free", "genius", "hidden", "honest", "huge", "illegal",
        "impossible", "insane", "instantly", "last", "legendary", "little",
        "mistake", "myth", "never", "new", "nobody", "only", "perfect",
        "proven", "real", "regret", "ruined", "secret", "shocking", "simple",
        "stop", "stupid", "surprising", "terrible", "truth", "ultimate",
        "unbelievable", "warning", "weird", "worst", "wrong",
    }
)

_EMOJI = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0001f1e6-\U0001f1ff"
    "\U00002b00-\U00002bff"
    "]",
    flags=re.UNICODE,
)
_WORD = re.compile(r"[A-Za-z0-9']+")
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
        "how", "i", "in", "is", "it", "my", "of", "on", "or", "that", "the",
        "this", "to", "was", "what", "why", "with", "you", "your",
    }
)


@dataclass
class TitleFeatures:
    """Deterministic, free, always available."""

    title: str
    char_count: int
    word_count: int
    truncation_risk: bool
    has_number: bool
    has_brackets: bool
    has_question: bool
    has_colon: bool
    allcaps_word_count: int
    emoji_count: int
    power_words: list[str] = field(default_factory=list)
    opening_ngram: str | None = None

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "char_count": self.char_count,
            "word_count": self.word_count,
            "truncation_risk": self.truncation_risk,
            "has_number": self.has_number,
            "has_brackets": self.has_brackets,
            "has_question": self.has_question,
            "has_colon": self.has_colon,
            "allcaps_word_count": self.allcaps_word_count,
            "emoji_count": self.emoji_count,
            "power_words": self.power_words,
            "opening_ngram": self.opening_ngram,
        }


def extract_features(title: str) -> TitleFeatures:
    """Compute every deterministic feature of a title.

    >>> f = extract_features("I Tried the 5 WORST Cameras (Shocking Results)")
    >>> f.word_count, f.has_number, f.has_brackets
    (8, True, True)
    >>> f.allcaps_word_count
    1
    >>> f.power_words
    ['shocking', 'worst']
    >>> f.opening_ngram
    'i tried the'

    Truncation risk keys off character count, not words:

    >>> extract_features("Short one").truncation_risk
    False
    >>> extract_features("A" * 80).truncation_risk
    True

    A single-word title still produces a usable opening n-gram:

    >>> extract_features("Unbelievable").opening_ngram
    'unbelievable'
    >>> extract_features("").word_count
    0
    """
    text = title.strip()
    words = _WORD.findall(text)
    lowered = {w.lower() for w in words}

    # An all-caps word only counts when it's genuinely emphasis — "I" and "A"
    # are capitalised by grammar, and two-letter acronyms are too ambiguous.
    allcaps = sum(1 for w in words if len(w) > 2 and w.isupper())

    opening = " ".join(w.lower() for w in words[:3]) if words else None

    return TitleFeatures(
        title=text,
        char_count=len(text),
        word_count=len(words),
        truncation_risk=len(text) > TRUNCATION_CHARS,
        has_number=any(c.isdigit() for c in text),
        has_brackets=bool(re.search(r"[\(\[\{].+[\)\]\}]", text)),
        has_question="?" in text,
        has_colon=":" in text,
        allcaps_word_count=allcaps,
        emoji_count=len(_EMOJI.findall(text)),
        power_words=sorted(lowered & POWER_WORDS),
        opening_ngram=opening,
    )


@dataclass
class TitleComparison:
    """One measurable difference between two sets of titles.

    Every field is populated so the UI can render a sentence rather than a
    number: "outliers average 41 characters, yours average 58".
    """

    metric: str
    label: str
    reference_value: float
    subject_value: float
    #: Signed difference, subject minus reference.
    delta: float
    #: Plain-language reading. Empty when the difference is too small to mean
    #: anything, which is itself worth showing.
    reading: str

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "label": self.label,
            "reference_value": round(self.reference_value, 2),
            "subject_value": round(self.subject_value, 2),
            "delta": round(self.delta, 2),
            "reading": self.reading,
        }


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def compare_title_sets(
    reference_titles: Sequence[str],
    subject_titles: Sequence[str],
    *,
    reference_label: str = "outliers",
    subject_label: str = "your videos",
    min_sample: int = 5,
) -> list[TitleComparison]:
    """Contrast two groups of titles across every deterministic feature.

    Returns an empty list when either group is too small to say anything —
    reporting that four titles average 6% more brackets than three others would
    be noise dressed as insight.

    >>> ref = ["Why X Fails", "How I Fixed X", "The X Mistake", "X Is Dead", "Stop Doing X"]
    >>> sub = ["A Very Long Complete Guide To Understanding X In Detail"] * 5
    >>> comps = compare_title_sets(ref, sub)
    >>> next(c for c in comps if c.metric == "char_count").delta > 0
    True
    >>> compare_title_sets(ref, ["one"])
    []
    """
    if len(reference_titles) < min_sample or len(subject_titles) < min_sample:
        return []

    ref = [extract_features(t) for t in reference_titles]
    sub = [extract_features(t) for t in subject_titles]

    specs: list[tuple[str, str, str, float]] = [
        # metric, label, unit-phrase, threshold below which we say nothing
        ("char_count", "Title length", "characters", 5.0),
        ("word_count", "Word count", "words", 1.0),
        ("allcaps_word_count", "ALL-CAPS words", "per title", 0.3),
        ("emoji_count", "Emoji", "per title", 0.3),
    ]

    comparisons: list[TitleComparison] = []
    for metric, label, unit, threshold in specs:
        ref_value = _mean(getattr(f, metric) for f in ref)
        sub_value = _mean(getattr(f, metric) for f in sub)
        delta = sub_value - ref_value
        if abs(delta) < threshold:
            reading = f"Effectively the same — no meaningful gap in {label.lower()}."
        else:
            direction = "more" if delta > 0 else "fewer"
            reading = (
                f"{subject_label.capitalize()} average {abs(delta):.1f} {direction} "
                f"{unit} than {reference_label} ({sub_value:.1f} vs {ref_value:.1f})."
            )
        comparisons.append(
            TitleComparison(metric, label, ref_value, sub_value, delta, reading)
        )

    # Boolean features are reported as prevalence rates.
    for metric, label in [
        ("has_number", "Contains a number"),
        ("has_brackets", "Uses brackets or parentheses"),
        ("has_question", "Asks a question"),
        ("truncation_risk", "At risk of being truncated"),
    ]:
        ref_rate = _mean(float(getattr(f, metric)) for f in ref) * 100
        sub_rate = _mean(float(getattr(f, metric)) for f in sub) * 100
        delta = sub_rate - ref_rate
        if abs(delta) < 15:
            reading = f"Similar rates — {label.lower()} isn't a differentiator here."
        else:
            direction = "more often" if delta > 0 else "less often"
            reading = (
                f"{sub_rate:.0f}% of {subject_label} vs {ref_rate:.0f}% of "
                f"{reference_label} — you do this {direction}."
            )
        comparisons.append(
            TitleComparison(metric, label, ref_rate, sub_rate, delta, reading)
        )

    return comparisons


def common_openings(titles: Sequence[str], *, top_n: int = 8, min_count: int = 2) -> list[dict]:
    """Most frequent opening phrases across a set of titles.

    Surfaces formula — "How I…", "I Tried…", "Why Your…" — which is usually the
    most copyable thing about a set of successful titles.

    >>> titles = ["How I Built X", "How I Built Y", "How I Broke Z", "Why X Fails"]
    >>> common_openings(titles, min_count=2)
    [{'phrase': 'how i', 'count': 3}]
    """
    counter: Counter[str] = Counter()
    for title in titles:
        words = [w.lower() for w in _WORD.findall(title)]
        if len(words) >= 2:
            counter[" ".join(words[:2])] += 1
    return [
        {"phrase": phrase, "count": count}
        for phrase, count in counter.most_common(top_n)
        if count >= min_count
    ]


def frequent_terms(titles: Sequence[str], *, top_n: int = 20) -> list[dict]:
    """Content words that recur across a set of titles, stopwords removed.

    >>> frequent_terms(["The Best Camera", "Best Camera Ever", "A Cheap Camera"], top_n=2)
    [{'term': 'camera', 'count': 3}, {'term': 'best', 'count': 2}]
    """
    counter: Counter[str] = Counter()
    for title in titles:
        for word in _WORD.findall(title.lower()):
            if len(word) > 2 and word not in _STOPWORDS:
                counter[word] += 1
    return [{"term": t, "count": c} for t, c in counter.most_common(top_n)]


def analyse_and_store(session: Session, videos: Sequence[Video]) -> list[TitleAnalysis]:
    """Compute and persist deterministic features for videos that lack them.

    Keyed on ``(video_id, title)``, so a retitled video gets a fresh row and the
    old analysis stays attached to the old title — which is what makes
    before/after comparison on a title change possible at all.
    """
    stored: list[TitleAnalysis] = []
    for video in videos:
        if not video.title:
            continue
        existing = session.scalar(
            select(TitleAnalysis).where(
                TitleAnalysis.video_id == video.id, TitleAnalysis.title == video.title
            )
        )
        if existing is not None:
            stored.append(existing)
            continue

        features = extract_features(video.title)
        row = TitleAnalysis(
            video_id=video.id,
            title=video.title,
            char_count=features.char_count,
            word_count=features.word_count,
            truncation_risk=features.truncation_risk,
            has_number=features.has_number,
            has_brackets=features.has_brackets,
            has_question=features.has_question,
            has_colon=features.has_colon,
            allcaps_word_count=features.allcaps_word_count,
            emoji_count=features.emoji_count,
            power_words=features.power_words,
            opening_ngram=features.opening_ngram,
        )
        session.add(row)
        stored.append(row)
    session.flush()
    return stored
