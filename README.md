# codewalk

Local MCP server that builds a **dependency graph** of a git repo and uses it for two things:

1. **Structural Q&A** — blast radius, cycles, reading order, architecture health, symbol lookup
2. **Pre-PR code review** — batched diffs with graph risk context and language/framework rubrics

Your AI agent (Cursor, Claude, Copilot, …) talks to codewalk over MCP. codewalk never calls an LLM and never edits files; the host agent does the reasoning and applies fixes.

## How it works

```
repo on disk
    → tree-sitter parse (13 languages)
    → DuckDB graph (files, imports, symbols, calls)
    → igraph (blast radius, PageRank, betweenness, cycles, shortest paths)
    → MCP tools over stdio
```

- Graph lives at `.codewalk/graph.duckdb` in the target repo.
- Review sessions live under `.codewalk/review_sessions/`.
- No vector store, no API keys, no network service — stdio MCP only.

**AST languages:** Python, JavaScript, TypeScript, Java, Go, Rust, Ruby, C, C++, C#, PHP, Kotlin, Swift.

## Install

Python 3.10+.

```bash
git clone <this-repo-url> codewalk
cd codewalk
python -m venv .venv && source .venv/bin/activate
pip install -e .
# or: pip install -r requirements.txt && pip install -e . --no-deps
```

Dev tooling:

```bash
pip install -e ".[dev]"
# or: pip install -r requirements-dev.txt
pre-commit install
```

## MCP setup

Copy [mcp.json.example](mcp.json.example) into your host’s config and set `cwd` to the repo you want analyzed.

| Host | Config location / key |
|---|---|
| VS Code | `.vscode/mcp.json` → `servers` |
| Cursor | `.cursor/mcp.json` → `mcpServers` |
| Claude Desktop | `claude_desktop_config.json` → `mcpServers` |

```json
{
  "mcpServers": {
    "codewalk": {
      "command": "python",
      "args": ["-m", "codewalk.mcp.server"],
      "cwd": "/absolute/path/to/the/repo/you/want/to/analyze"
    }
  }
}
```

Use the venv’s `python` if the host won’t see your PATH. Every tool also accepts an optional `repo_path` to override `cwd`.

## Typical usage

**Analyze / ask about structure**

1. Graph builds automatically on first query (or call `codewalk_analyze_codebase`).
2. Ask things like: overview, blast radius of a file, circular deps, reading order, call chain.
3. Optional once per repo: `codewalk_get_stack_info` → agent saves stack via `codewalk_save_stack_context` for richer overviews and better review rubrics.

**Review changes**

Review needs an explicit target — codewalk will not assume `main`/`master`.

| You want | Pass |
|---|---|
| Local work on this branch (staged + unstaged + untracked) | `target_branch="current"` |
| Commits + uncommitted work vs a base branch | `target_branch="main"` (or `develop`, …) |
| Staged only | `staged=True` |
| One commit | `commit="<sha>"` |

If the agent calls review with no target, the tool returns a prompt to ask you which branch to use.

Flow:

1. `codewalk_run_review(target_branch=...)` → first batch (diff + risk + rubrics)
2. Agent reviews → `codewalk_submit_batch_findings`
3. `codewalk_review_next_batch` until done
4. `codewalk_get_review_summary`
5. Accept/reject findings in the session’s `llm_findings.json`
6. `codewalk_accept_and_verify_fix` → agent applies accepted fixes, then `codewalk_run_static_analysis` / `codewalk_run_tests`

Prompt cheat sheet: [MCP_EXAMPLES.md](MCP_EXAMPLES.md). Why local review exists: [REVIEW_BRIEF.md](REVIEW_BRIEF.md).

## Tools (25)

| Category | Tools |
|---|---|
| Setup | `codewalk_analyze_codebase`, `codewalk_refresh_analysis`, `codewalk_generate_config` |
| Query | `codewalk_get_module_info`, `codewalk_explain_function`, `codewalk_explain_class`, `codewalk_lookup_symbol`, `codewalk_get_overview`, `codewalk_get_blast_radius_map`, `codewalk_find_circular_dependencies`, `codewalk_get_reading_order`, `codewalk_get_execution_flow` |
| Architecture | `codewalk_get_architecture_health`, `codewalk_call_chain` |
| Stack | `codewalk_get_stack_info`, `codewalk_save_stack_context` |
| Review | `codewalk_run_review`, `codewalk_re_review`, `codewalk_review_next_batch`, `codewalk_submit_batch_findings`, `codewalk_get_review_summary`, `codewalk_get_review_details`, `codewalk_accept_and_verify_fix` |
| Maintenance | `codewalk_run_static_analysis`, `codewalk_run_tests` |

## Configuration

Optional. Missing config = defaults.

- `codewalk.yaml` — excludes/includes, language overrides, static-analysis and test commands. Generate a starter with `codewalk_generate_config`.
- `.codewalkignore` — gitignore syntax; merged with `.gitignore`.
- `.codewalk/stack_context.json` — optional host-written stack metadata.

## Development

```bash
pytest                      # coverage via pyproject addopts
ruff check src tests
ruff format src tests
mypy --strict src/codewalk
pre-commit run --all-files
```

CI runs on Python 3.10–3.12 (lint, format, mypy, pytest with ≥90% coverage).

## What this is not

- Not a knowledge-graph / docs / PDF indexer
- Not a vector / embedding search engine
- Not a hosted API — no auth, no multi-tenant server
- Does not call LLMs or edit your files over MCP

## License

MIT (see `pyproject.toml`)
