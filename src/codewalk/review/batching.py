"""Token-bounded grouping of changed files into review batches.

Greedily packs diff files into batches that fit a token budget, so a single
review pass over a large PR doesn't blow past the host LLM's context window.
A single file that alone exceeds the budget still gets its own batch rather
than being dropped or split.
"""

from __future__ import annotations

from codewalk.review.diff_parser import DiffFile

_DEFAULT_MAX_TOKENS_PER_BATCH = 50_000
_DEFAULT_FILE_TOKEN_CAP = 10_000
_FIXED_OVERHEAD_TOKENS = 50  # headers/formatting added by build_batch_context per file
_CHARS_PER_TOKEN = 3  # same rough heuristic used by context_builder.estimate_tokens


def estimate_file_tokens(diff_file: DiffFile, file_token_cap: int = _DEFAULT_FILE_TOKEN_CAP) -> int:
    """Estimate the prompt tokens one file contributes to a batch.

    Diff hunks are always included in full; file content is capped at
    `file_token_cap` by `context_builder.smart_truncate_file_content`.
    """
    hunk_chars = sum(len(line.content) for hunk in diff_file.hunks for line in hunk.lines)
    hunk_tokens = hunk_chars // _CHARS_PER_TOKEN
    file_tokens = min(file_token_cap, (diff_file.added_lines + diff_file.removed_lines + 200) * 5)
    return file_tokens + hunk_tokens + _FIXED_OVERHEAD_TOKENS


def estimate_batch_tokens(
    batch: list[DiffFile], base_tokens: int = 0, file_token_cap: int = _DEFAULT_FILE_TOKEN_CAP
) -> int:
    """Estimate total tokens for a batch: shared `base_tokens` plus each file's contribution."""
    return base_tokens + sum(estimate_file_tokens(df, file_token_cap) for df in batch)


def estimate_shared_context_tokens(*text_blocks: str) -> int:
    """Estimate tokens for content repeated in every batch (stack header, rubrics).

    `start_review` should compute this once per review and pass it as
    `base_tokens` to `make_batches`/`estimate_batch_tokens` -- otherwise the
    token budget only accounts for per-file diff/content costs and can be
    blown well past `max_tokens_per_batch` once the shared rubric and stack
    text is actually assembled into the batch context.
    """
    total_chars = sum(len(block) for block in text_blocks if block)
    return total_chars // _CHARS_PER_TOKEN


def make_batches(
    diff_files: list[DiffFile],
    max_tokens_per_batch: int = _DEFAULT_MAX_TOKENS_PER_BATCH,
    base_tokens: int = 0,
    file_token_cap: int = _DEFAULT_FILE_TOKEN_CAP,
) -> list[list[DiffFile]]:
    """Greedily group diff files into batches that fit `max_tokens_per_batch`.

    A file whose own estimate already exceeds the budget is placed alone in
    its own (over-budget) batch rather than being split or dropped.
    """
    batches: list[list[DiffFile]] = []
    current_batch: list[DiffFile] = []

    for diff_file in diff_files:
        trial_batch = [*current_batch, diff_file]
        tokens = estimate_batch_tokens(trial_batch, base_tokens, file_token_cap)
        if current_batch and tokens > max_tokens_per_batch:
            batches.append(current_batch)
            current_batch = [diff_file]
        else:
            current_batch = trial_batch

    if current_batch:
        batches.append(current_batch)

    return batches
