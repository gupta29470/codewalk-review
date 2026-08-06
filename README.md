<p align="center">
  <h1 align="center">CODEWALK</h1>
   <p align="center" style="font-size:1.5em; font-weight:700;">
     <a href="https://www.codewalk.xyz/">Landing page & docs →</a>
   </p>
  <p align="center">
    <strong>AI-powered codebase intelligence tool</strong><br>
  </p>
</p>

Local MCP server that builds a **dependency graph** of a git repo and uses it for two things:

1. **Structural Q&A** — blast radius, cycles, reading order, architecture health, symbol lookup
2. **Pre-PR code review** — batched diffs with graph risk context and language/framework rubrics

Your AI agent (Cursor, Claude, Copilot, …) talks to codewalk over MCP. codewalk never calls an LLM and never edits files; the host agent does the reasoning and applies fixes.

<p align="center">
  <a href="#how-it-works">How it works</a> •
  <a href="#-demo">Demo</a> •
  <a href="#-code-review--powered-by-the-intelligence-layer">Code Review</a> •
  <a href="#install">Install</a> •
  <a href="#mcp-setup">MCP Setup</a> •
  <a href="#typical-usage">Usage</a> •
  <a href="#tools-25">Tools</a> •
  <a href="#configuration">Configuration</a> •
  <a href="#development">Development</a>
</p>

---

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

---

## 🎬 Demo

### MCP — Overview

https://github.com/user-attachments/assets/d65d23c6-38bc-4610-b5d0-62669d85e5fd

### MCP — Explain Function

https://github.com/user-attachments/assets/252a4738-3a22-4759-94f3-0ac41f7f0c09

### MCP — Blast Radius

https://github.com/user-attachments/assets/052fa64b-e421-48ed-b65c-29609e0caf32

### MCP — Run Review

https://github.com/user-attachments/assets/de36cdff-610b-4f4b-a422-7cff737fef2f

---

## 🔬 Code Review — Powered by the Intelligence Layer

Codewalk's review engine is built on top of the codebase intelligence layer. It doesn't just lint — it understands your architecture, knows what files are risky, and reviews with full context.

### How it works

```
git diff → Static Analysis (graph risk, PageRank, cycles, blast radius)
         → Batch files (token-bounded, grouped by feature)
         → Host LLM reviews each batch with full context
         → Submit findings to disk per batch (JSON + Markdown, context stays clean)
         → Final summary: raw findings grouped by severity
```

You talk to your **IDE agent**; the agent calls **Codewalk MCP tools**. Codewalk does not render UI — each host has its own approve/reject experience (Cursor approval cards, Copilot chat, Claude Code prompts, etc.). The agent must present each fix and wait for your approval through that host UI (or yes/no in chat).

### What makes it different

| Capability | CodeRabbit / GitHub Copilot Review | Codewalk Review |
|---|---|---|
| **Architecture awareness** | ❌ No dependency graph | ✅ DuckDB + igraph: PageRank, fan-in, cycles, bottlenecks |
| **Blast radius** | ❌ | ✅ "This file has 23 callers — review with extra care" |
| **Works without indexing** | — | ✅ Just needs a git repo (graph enhances but isn't required) |
| **Batched for large PRs** | Dumps everything at once | ✅ Token-bounded batches, sorted by risk, host LLM stays focused |
| **Custom rubrics** | Limited | ✅ Per-language + per-framework + optional stack context |
| **Fix application** | Suggests only | ✅ Accept/reject → host applies → verify with tests |
| **Severity levels** | varies | `blocker` · `error` · `suggestion` |

### Zero-setup review

Review runs on **any git repo** — no prior `codewalk_analyze_codebase` needed. The dependency graph is built automatically on first review (~5s) and cached:

| Component | Auto (graph-only) |
|-----------|-------------------|
| Git diff + file content | ✅ |
| Rubrics + stack detection | ✅ (from file extensions / optional stack context) |
| Blast radius, PageRank, cycles | ✅ Built on-the-fly (~5s), then from cached DuckDB |
| Neighborhood (callers, related files) | ✅ From the graph |

### Severity levels

| Level | Value | Meaning |
|-------|-------|---------|
| **Blocker** | `"blocker"` | Must fix before merge — blocks the PR |
| **Error** | `"error"` | Should fix — real bugs, logic errors, security risks |
| **Suggestion** | `"suggestion"` | Nice to have — style, naming, minor improvements |

### Review target (required)

Review needs an explicit target — codewalk will not assume `main`/`master`. If the agent calls review with no target, the tool returns a prompt to ask you which branch to use.

| You want | Pass |
|---|---|
| Local work on this branch (staged + unstaged + untracked) | `target_branch="current"` |
| Commits + uncommitted work vs a base branch | `target_branch="main"` (or `develop`, …) |
| Staged only | `staged=True` |
| One commit | `commit="<sha>"` |

### MCP review flow

1. `codewalk_run_review(target_branch=...)` → session + first batch (diff + risk + rubrics)
2. Host reviews batch → `codewalk_submit_batch_findings(session_id, [...])` → saved to disk as JSON; a Markdown companion is also written for easy reading
3. `codewalk_review_next_batch(session_id)` → next batch (context window is clean)
4. Repeat until all batches done
5. `codewalk_get_review_summary(session_id)` → structured summary of raw findings + verdict guidance (`request_changes` if any BLOCKING finding, else `approve`)
6. User edits `llm_findings.json` in the session folder → sets `user_verdict` to `accepted` / `rejected` per finding
7. `codewalk_accept_and_verify_fix(session_id)` returns the accepted findings → the host applies them with its own editing tools, then verifies with `codewalk_run_static_analysis` + `codewalk_run_tests`
8. (Optional) `codewalk_re_review(target_branch=...)` → fresh review that hides previously rejected findings

**Finding shape** for `codewalk_submit_batch_findings`:

| Field | Required | Notes |
|-------|----------|-------|
| `severity` | ✅ | `'blocker'` \| `'error'` \| `'suggestion'` |
| `category` | ✅ | `'bug'` \| `'security'` \| `'style'` \| `'test'` \| `'blast_radius'` \| `'design'` \| `'naming'` \| `'complexity'` \| `'error_handling'` \| `'type_safety'` \| `'architecture'` \| `'logging'` \| `'privacy'` \| `'hygiene'` |
| `file_path` | ✅ | Path relative to repo root |
| `title` | ✅ | Short finding title |
| `explanation` | ✅ | Why it matters |
| `line_number` | | Optional |
| `current_code` | | Optional |
| `recommended_code` | | Optional |
| `blocking` | | Bool, default `false` |

An empty `findings` list is valid (means the batch is clean).

### Review & approve fixes (agent + MCP)

1. Agent runs `codewalk_run_review` (returns enriched context for the host LLM to review)
2. Agent reviews each batch and calls `codewalk_submit_batch_findings`
3. After all batches: `codewalk_get_review_summary`
4. User edits `llm_findings.json`: set `user_verdict` to `accepted` or `rejected` for each finding
5. Apply + verify accepted fixes: `codewalk_accept_and_verify_fix(session_id)` returns every accepted finding with instructions — the host LLM applies them with its own editing tools, then verifies with `codewalk_run_static_analysis` + `codewalk_run_tests`. **Codewalk never edits files over MCP.**

**Example:** `@codewalk review my changes against main, then fix each issue only after I approve`

### Natural-language prompts (review)

#### "Review my changes for bugs"

**Tool:** `codewalk_run_review` — requires an explicit target (see table above)

```
@codewalk review my changes
@codewalk review my local work
@codewalk_run_review target_branch="current"
@codewalk_run_review target_branch="main"
@codewalk_run_review staged=true target_branch="main"
```

**When to use:** Before pushing a PR. Codewalk gathers the full diff, neighborhood context, blast radius, and stack signals, then returns them so the host model can perform the review directly — no separate LLM inside codewalk.

#### "I've addressed the feedback — review again"

**Tool:** `codewalk_re_review`

```
@codewalk I've addressed the feedback — review it again against main
@codewalk_re_review target_branch="main"
```

Starts a fresh session and hides findings you previously rejected.

#### "Summarize / status of the review"

```
@codewalk summarize the review findings
@codewalk_get_review_summary <session_id>

@codewalk what's the status of that review session?
@codewalk_get_review_details <session_id>
```

#### "Apply the fixes I accepted"

```
@codewalk apply and verify the fixes I accepted
@codewalk_accept_and_verify_fix <session_id>
```

Then the host applies accepted findings and runs:

```
@codewalk run static analysis on the files I just changed
@codewalk_run_static_analysis <paths>

@codewalk run the tests
@codewalk_run_tests <paths>
```

### Review quick reference

| You want to... | Just say... |
|---|---|
| Review local work on this branch | `@codewalk review my local work` → `codewalk_run_review(target_branch="current")` |
| Review vs a base branch | `@codewalk review my changes against main` → `codewalk_run_review(target_branch="main")` |
| Review staged only | `@codewalk review staged changes against main` → `staged=True` |
| Re-review after fixes | `@codewalk review again against main` → `codewalk_re_review` |
| Accept/reject findings | Edit `llm_findings.json` → set `user_verdict` |
| Apply accepted fixes | `@codewalk apply and verify fixes` → `codewalk_accept_and_verify_fix` → host applies + verifies |
| Run static analysis | `@codewalk run static analysis on src/auth.py` |
| Run tests | `@codewalk run tests for src/auth.py` |

Prompt cheat sheet (all tools): [MCP_EXAMPLES.md](MCP_EXAMPLES.md).

---

## Install

Python 3.10+.

```bash
git clone https://github.com/gupta29470/codewalk-review.git
cd codewalk-review
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

> **One repo per MCP server process.** Codewalk keeps runtime state (graph, repo path) in memory. Pointing the same running MCP server at multiple repos — or rapidly switching workspaces in the same process — can overwrite that state. Use one editor window / one MCP connection per repo. The stdio transport is safe because each connection spawns a separate process.

## Typical usage

**Analyze / ask about structure**

1. Graph builds automatically on first query (or call `codewalk_analyze_codebase`).
2. Ask things like: overview, blast radius of a file, circular deps, reading order, call chain.
3. Optional once per repo: `codewalk_get_stack_info` → agent saves stack via `codewalk_save_stack_context` for richer overviews and better review rubrics.

**Review changes** — see [Code Review](#-code-review--powered-by-the-intelligence-layer) above.

## Tools (25)

| Category | Tools |
|---|---|
| Setup | `codewalk_analyze_codebase`, `codewalk_refresh_analysis`, `codewalk_generate_config` |
| Query | `codewalk_get_module_info`, `codewalk_explain_function`, `codewalk_explain_class`, `codewalk_lookup_symbol`, `codewalk_get_overview`, `codewalk_get_blast_radius_map`, `codewalk_find_circular_dependencies`, `codewalk_get_reading_order`, `codewalk_get_execution_flow` |
| Architecture | `codewalk_get_architecture_health`, `codewalk_call_chain` |
| Stack | `codewalk_get_stack_info`, `codewalk_save_stack_context` |
| Review | `codewalk_run_review`, `codewalk_re_review`, `codewalk_review_next_batch`, `codewalk_submit_batch_findings`, `codewalk_get_review_summary`, `codewalk_get_review_details`, `codewalk_accept_and_verify_fix` |
| Maintenance | `codewalk_run_static_analysis`, `codewalk_run_tests` |

### MCP tools — index / graph requirements

| Tool | Graph required? | Notes |
|------|-----------------|-------|
| `codewalk_analyze_codebase` | Builds/loads | Persistent DuckDB graph |
| `codewalk_generate_config` | No | Creates starter `codewalk.yaml` |
| Query tools (overview, modules, symbols, …) | Yes | Auto-builds/loads graph |
| `codewalk_find_circular_dependencies` | Yes | Uses graph data |
| `codewalk_get_architecture_health` | Yes | Graph stats + cycles |
| `codewalk_run_review`, `codewalk_re_review`, `codewalk_get_stack_info` | Soft / Yes | Better with graph; review builds graph on demand |
| `codewalk_get_review_details` | Session on disk | Reads persisted session |
| `codewalk_accept_and_verify_fix` | Session on disk | Returns accepted findings; host applies them itself |
| `codewalk_run_static_analysis` | No | ruff/mypy/eslint/etc. |
| `codewalk_run_tests` | No | pytest/npm test/etc. |

## Configuration

Optional. Missing config = defaults.

- `codewalk.yaml` — excludes/includes, language overrides, static-analysis and test commands. Generate a starter with `codewalk_generate_config`.
- `.codewalkignore` — gitignore syntax; merged with `.gitignore`.
- `.codewalk/stack_context.json` — optional host-written stack metadata (richer overview + better review rubrics).

Example `codewalk.yaml`:

```yaml
indexing:
  exclude:
    - tests/**
    - docs/**
    - "*.generated.*"
  include:
    - docs/architecture/**
```

For language/framework-specific review rubrics, place `.md` files in `.codewalk/rubrics/` (e.g. `.codewalk/rubrics/python.md`, `.codewalk/rubrics/python_fastapi.md`, `.codewalk/rubrics/core.md`). These override built-in rubrics.

### Adding `.codewalk/` to `.gitignore`

Codewalk stores graph and review data inside each target repo at `.codewalk/`. This directory should **not** be committed:

```gitignore
# Codewalk index (auto-generated)
.codewalk/
```

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
