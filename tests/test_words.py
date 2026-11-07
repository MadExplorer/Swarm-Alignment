"""The game word list is disjoint from all task-data vocabulary, so a handed-out word can never appear in a
task's files or instruction (which the word judge would flag and the labels would count as a leak)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "upstream"))

from cover_tasks import task_vocabulary  # noqa: E402
from scoring import WORDS  # noqa: E402


def test_no_word_occurs_in_task_data():
    vocab = task_vocabulary()
    clash = sorted(w for w in WORDS if w.lower() in vocab)
    assert clash == [], clash


def test_vocab_covers_the_known_source_lists_and_old_clashes():
    vocab = task_vocabulary()
    # the two words removed from the list because they are task vocabulary (kettle: a product; valley: a region)
    assert "kettle" in vocab and "valley" in vocab
    # tokens that always appear in task data
    for token in ("part", "orders", "region", "status", "answer"):
        assert token in vocab, token


def test_word_list_is_still_big_enough_and_clean():
    assert len(WORDS) == len(set(WORDS)) >= 5 * 10        # 5 agents x 10 words per round, disjoint
    assert all(w.islower() and w.isalpha() for w in WORDS)
    assert "kettle" not in WORDS and "valley" not in WORDS
