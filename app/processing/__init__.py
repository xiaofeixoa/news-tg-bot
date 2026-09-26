"""News processing stages: normalise, dedup, classify, score, summarise, tag."""

from app.processing import (  # noqa: F401
    classifier,
    deduplicate,
    normalize,
    scorer,
    summarizer,
    tagger,
)

__all__ = ["classifier", "deduplicate", "normalize", "scorer", "summarizer", "tagger"]
