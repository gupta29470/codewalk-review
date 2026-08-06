# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] — Initial release

Clean-room, MCP-only reimplementation of the graph-analysis and code-review
subsystems from upstream `codewalk` — see [README.md](README.md) for what's
deliberately in and out of scope.

### Added

- **Graph system**: repo scanning (tree-sitter, 13 languages) with a
  configurable `.gitignore`/`.codewalkignore`/`codewalk.yaml`-aware safety
  net; a persistent DuckDB graph (files, imports, symbols, symbol calls,
  modules); an in-memory igraph runtime for blast radius, PageRank,
  betweenness centrality, cycle detection, and shortest import paths.
- **Query/explain layer**: deterministic symbol lookup and explanation
  (`codewalk_explain_function`/`codewalk_explain_class`/`codewalk_lookup_symbol`),
  module info, project overview, blast radius map, circular dependency
  detection, reading order, execution flow, architecture health report, and
  import call-chain tracing — all backed directly by the DuckDB graph, no
  LLM calls.
- **Review system**: batched, host-LLM-driven code review
  (`codewalk_run_review` → `codewalk_review_next_batch` →
  `codewalk_submit_batch_findings` → `codewalk_get_review_summary` →
  `codewalk_accept_and_verify_fix`), with git diff parsing, deterministic
  risk annotations, stack-aware rubric selection (35 rubric files ported),
  neighborhood context (callers/tests), token-bounded batching that accounts
  for shared per-batch context (stack header + rubrics), and session
  persistence (JSON + human-readable Markdown companions).
- **Stack context**: optional, host-authored `.codewalk/stack_context.json`
  (`codewalk_get_stack_info` → `codewalk_save_stack_context`) that enriches
  `codewalk_get_overview`/`codewalk_get_architecture_health` output and
  improves review rubric selection — never a hard requirement.
- **Re-review**: `codewalk_re_review` starts a fresh session while hiding
  findings the user previously rejected.
- **Maintenance tools**: `codewalk_run_static_analysis` and
  `codewalk_run_tests`, language-aware and configurable per-project via
  `codewalk.yaml`.
- **Workspace registry**: multi-repo-aware in-process cache with staleness
  detection, so switching between repos in the same MCP session picks up
  the right graph without a restart.
- **Hardening**: path-traversal guards on every path-accepting tool
  argument, thread-safe concurrent DuckDB access, graceful handling of
  corrupted `.codewalk/` state, malformed `codewalk.yaml`, non-UTF8 content,
  CRLF/LF diffs, and XSS-inert Markdown rendering. ~93% test coverage on
  `analysis/`, `graph/`, `review/`.
- **MCP server**: 25 tools over stdio transport (`mcp.server.fastmcp`), with
  a scoped `instructions=` block describing exactly the tools this server
  supports (no references to features that don't exist here).

### Notes on parity with upstream

A systematic, tool-by-tool comparison against the upstream reference
implementation was run after the initial build to catch behavioral gaps in
the overlapping feature set (excluding the deliberately-out-of-scope
subsystems). Findings from that pass, each verified by direct functional
testing before being applied:

- `codewalk_get_overview`/`codewalk_get_architecture_health` now show a
  "Declared Architecture" header when stack context has been saved.
- Batch token budgeting accounts for shared context (stack header + rubrics),
  not just per-file diff size.
- The Markdown findings renderer surfaces `evidence` and `verifier_notes`
  fields that exist on the `Finding` model but were previously dropped.
- `.codewalkignore` is read and merged with `.gitignore` patterns.
- `codewalk_generate_config` detects the repo's tech stack and layers
  stack-specific excludes automatically.
- The MCP error boundary treats the query layer's `ValueError` (used
  intentionally for "not found"/invalid-input outcomes) the same as typed
  `CodewalkError`s, instead of logging them as unexpected failures.
- `codewalk_get_review_summary` now renders a "Review Coverage" section
  (batches with findings / clean-justified / not-reviewed) from data that
  was already being recorded in `batch_state.json` but never surfaced.

Two intentional design differences from upstream, left as-is:

- `codewalk_review_next_batch` does not hard-block advancing past a batch
  that was never submitted (upstream gates this).
- `codewalk_submit_batch_findings` does not require a `notes` justification
  for an empty (clean) `findings` list (upstream requires one).
