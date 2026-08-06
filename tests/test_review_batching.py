"""Tests for review.batching: token-bounded grouping of changed files."""

from __future__ import annotations

from codewalk.review.batching import estimate_batch_tokens, estimate_file_tokens, make_batches
from tests.conftest import make_diff_file


def test_make_batches_zero_files_returns_empty_list() -> None:
    assert make_batches([]) == []


def test_make_batches_single_small_file_one_batch() -> None:
    diff_files = [make_diff_file("a.py", added=["x = 1"])]
    batches = make_batches(diff_files, max_tokens_per_batch=50_000)
    assert len(batches) == 1
    assert batches[0] == diff_files


def test_make_batches_one_huge_file_bigger_than_budget_gets_its_own_batch() -> None:
    huge = make_diff_file("huge.py", added=[f"line_{i}" for i in range(10_000)])
    small = make_diff_file("small.py", added=["x = 1"])

    batches = make_batches([huge, small], max_tokens_per_batch=100)
    assert len(batches) == 2
    assert batches[0] == [huge]
    assert batches[1] == [small]


def test_make_batches_n_files_exactly_at_boundary_splits_correctly() -> None:
    # Each file contributes the same estimate; pick a budget that fits exactly two.
    files = [make_diff_file(f"f{i}.py", added=["x = 1"]) for i in range(4)]
    per_file = estimate_file_tokens(files[0])
    budget = per_file * 2  # room for exactly 2 files per batch

    batches = make_batches(files, max_tokens_per_batch=budget)
    assert sum(len(b) for b in batches) == 4
    for batch in batches:
        assert len(batch) <= 2


def test_make_batches_all_files_fit_in_one_batch_with_large_budget() -> None:
    files = [make_diff_file(f"f{i}.py", added=["x = 1"]) for i in range(20)]
    batches = make_batches(files, max_tokens_per_batch=10_000_000)
    assert len(batches) == 1
    assert len(batches[0]) == 20


def test_make_batches_preserves_file_order() -> None:
    files = [make_diff_file(f"f{i}.py", added=["x = 1"]) for i in range(5)]
    batches = make_batches(files, max_tokens_per_batch=10_000_000)
    flattened = [df for batch in batches for df in batch]
    assert flattened == files


def test_estimate_file_tokens_scales_with_hunk_size() -> None:
    small = make_diff_file("a.py", added=["x = 1"])
    large = make_diff_file("b.py", added=["x = 1" * 100] * 50)
    assert estimate_file_tokens(large) > estimate_file_tokens(small)


def test_estimate_file_tokens_respects_file_token_cap() -> None:
    diff_file = make_diff_file("a.py", added=["x = 1"] * 5000)
    low_cap = estimate_file_tokens(diff_file, file_token_cap=10)
    high_cap = estimate_file_tokens(diff_file, file_token_cap=10_000)
    assert low_cap < high_cap


def test_estimate_batch_tokens_includes_base_tokens() -> None:
    files = [make_diff_file("a.py", added=["x = 1"])]
    without_base = estimate_batch_tokens(files, base_tokens=0)
    with_base = estimate_batch_tokens(files, base_tokens=500)
    assert with_base == without_base + 500


def test_estimate_batch_tokens_empty_batch_is_just_base() -> None:
    assert estimate_batch_tokens([], base_tokens=42) == 42
