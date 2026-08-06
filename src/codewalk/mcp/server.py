"""FastMCP server: the only MCP-facing entry point for codewalk.

Every tool here is a thin wrapper: resolve the repo, delegate to the
`workspace`/`query`/`review` layer, and format the result (or a caught typed
error) as a string. No business logic lives in this module.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any, ParamSpec

from mcp.server.fastmcp import FastMCP

from codewalk.codewalk_config import generate_default_config, load_codewalk_yaml
from codewalk.errors import CodewalkError
from codewalk.ingestion.scanner import scan_repo
from codewalk.log import get_logger
from codewalk.mcp import exec_tools
from codewalk.paths import resolve_within_repo
from codewalk.query import query
from codewalk.repo_discovery import resolve_repo_root
from codewalk.review import engine
from codewalk.review.stack_detect import (
    AVAILABLE_RUBRICS,
    STACK_DETECT_PROMPT,
    format_stack_context_header,
    load_cached_stack_context,
    save_stack_context,
)
from codewalk.review.target import (
    format_ask_for_review_target,
    is_current_branch_alias,
    needs_review_target,
)
from codewalk.staleness import install_github_staleness_wrappers
from codewalk.workspace import Workspace, WorkspaceRegistry

logger = get_logger(__name__)

mcp = FastMCP(
    "codewalk",
    instructions=(
        "Codewalk builds a persistent dependency graph of this repository "
        "(imports, symbols, calls) using tree-sitter and DuckDB, then answers "
        "structural questions directly from that graph -- instantly, and without "
        "re-reading or re-parsing files. Prefer these tools over manually grepping "
        "or reading files for anything about structure, dependencies, risk, or review.\n"
        "\n"
        "All tools accept an optional `repo_path` argument; it defaults to the "
        "nearest `.git` ancestor of the current working directory.\n"
        "\n"
        "## SETUP\n"
        "- `codewalk_generate_config(force?)` -- optional. Writes a starter "
        "`codewalk.yaml` with tech-stack-aware excludes (skip if not needed).\n"
        "- `codewalk_analyze_codebase(refresh?)` -- builds the dependency graph on "
        "first use; later calls reopen the persisted graph instantly. Pass "
        "`refresh=True` to force a full rescan after major changes.\n"
        "- `codewalk_refresh_analysis()` -- always forces a full rescan and rebuild.\n"
        "\n"
        "## ANSWERING QUESTIONS\n"
        "- 'What's in module/feature X?' -> `codewalk_get_module_info(module_name)`\n"
        "- 'What does function/method X do?' -> `codewalk_explain_function(function_name)`\n"
        "- 'What does class/component X do?' -> `codewalk_explain_class(class_name)`\n"
        "- 'Where is X defined?' / symbol lookup -> `codewalk_lookup_symbol(query_text)`\n"
        "- 'Give me an overview' / 'explain the architecture' -> `codewalk_get_overview()`\n"
        "- 'What breaks if I change X?' -> `codewalk_get_blast_radius_map(target)` -- "
        "`target` is a module name, a file name, or empty for the top riskiest files repo-wide\n"
        "- 'Are there circular dependencies?' -> `codewalk_find_circular_dependencies()`\n"
        "- 'Where should I start reading?' -> `codewalk_get_reading_order(module_name?)`\n"
        "- 'Show me the dependency flow' -> `codewalk_get_execution_flow(module_name?)` -- "
        "no argument gives module-to-module flow, a `module_name` gives file-to-file flow\n"
        "- 'How healthy is the architecture? What should I refactor first?' -> "
        "`codewalk_get_architecture_health()` -- bottlenecks, key files, cycles, priorities\n"
        "- 'How does X reach Y?' / import path -> `codewalk_call_chain(source, target)`\n"
        "\n"
        "## STACK CONTEXT (optional enrichment)\n"
        "`codewalk_get_overview` and `codewalk_get_architecture_health` show a richer "
        "'Declared Architecture' section, and code review picks better rubrics, when "
        "`.codewalk/stack_context.json` exists. It is never required -- everything "
        "still works without it.\n"
        "To set it up (once per repo, persists across commits):\n"
        "1. Call `codewalk_get_stack_info()` -- returns the file tree + a detection prompt\n"
        "2. Analyze it yourself and build a JSON object describing the stack "
        "(languages, frameworks, architecture, state_management, data_layer, etc.)\n"
        "3. Call `codewalk_save_stack_context(stack)` with that JSON object (a dict, "
        "not a JSON string) to persist it\n"
        "To refresh after the stack changes (e.g. a new framework was added), repeat "
        "steps 1-3.\n"
        "\n"
        "## CODE REVIEW\n"
        "Review is batched: changed files are grouped into token-bounded batches so "
        "you can review each thoroughly. You do the actual reviewing -- these tools "
        "only supply diff, risk, and rubric context and persist your findings.\n"
        "IMPORTANT: Never assume `main`, `master`, or any default base branch. If the "
        "user has not clearly said which branch to review against, ask them first "
        "(e.g. `main`, `develop`, or `current` for this branch's local work). Do not "
        "call `codewalk_run_review` / `codewalk_re_review` without a target until they "
        "answer; if you omit `target_branch`, the tool returns a clarification prompt.\n"
        "1. `codewalk_run_review(target_branch?, staged?, commit?)` -- starts a session "
        'and returns the first batch. Pass `target_branch="current"` to review this '
        "branch's local work (staged + unstaged + untracked). Pass "
        '`target_branch="<base>"` to review commits and uncommitted work against that '
        "base. Pass `staged=True` for staged-only, or `commit=` for a historical "
        "snapshot.\n"
        "2. Review the batch yourself for bugs, security issues, logic errors, and style.\n"
        "3. `codewalk_submit_batch_findings(session_id, findings, notes?)` -- persist "
        "findings for this batch. An empty `findings` list is valid (means the batch is clean).\n"
        "   Each finding dict needs: `severity` ('blocker'|'error'|'suggestion'), "
        "`category` ('bug'|'security'|'style'|'test'|'blast_radius'|'design'|'naming'|"
        "'complexity'|'error_handling'|'type_safety'|'architecture'|'logging'|'privacy'|"
        "'hygiene'), `file_path`, `title`, `explanation` -- all required. Optional: "
        "`line_number`, `current_code`, `recommended_code`, `blocking` (bool, default False).\n"
        "4. `codewalk_review_next_batch(session_id)` -- repeat steps 2-3 until all "
        "batches are done.\n"
        "5. `codewalk_get_review_summary(session_id)` -- combined architectural + "
        "review findings, plus verdict guidance (`request_changes` if any BLOCKING "
        "finding, else `approve`).\n"
        "6. Present findings to the user. They edit `llm_findings.json` in the session "
        "folder, setting `user_verdict` to `'accepted'` or `'rejected'` per finding.\n"
        "7. `codewalk_accept_and_verify_fix(session_id)` -- returns only the accepted "
        "findings. Apply those fixes yourself with your own editing tools, then verify "
        "with `codewalk_run_static_analysis` and `codewalk_run_tests` on the modified files.\n"
        "8. `codewalk_re_review(target_branch?, staged?, commit?)` -- start a fresh "
        "review after the user has addressed feedback; automatically hides findings "
        "they previously rejected.\n"
        "- `codewalk_get_review_details(session_id)` -- inspect a session's status and "
        "progress (repo, branch, findings counts, batches returned so far).\n"
        "\n"
        "## MAINTENANCE\n"
        "- `codewalk_run_static_analysis(file_paths)` -- runs the language-appropriate "
        "linter/type-checker configured in `codewalk.yaml` (e.g. ruff, go vet) on the "
        "given files.\n"
        "- `codewalk_run_tests(file_paths?)` -- runs the project's test suite; "
        "`file_paths` helps auto-detect which language's test command to use.\n"
        "\n"
        "## STALENESS\n"
        "If this codewalk install's local git branch falls behind its GitHub remote, "
        "any tool result may be prefixed with a one-line banner, e.g. 'This codewalk "
        "install is N commits behind ... Run `git pull` ... then restart the MCP "
        "server.' Relay that instruction to the user rather than ignoring it or "
        "retrying the call -- the banner text itself says exactly what to do.\n"
        "\n"
        "## ERROR HANDLING\n"
        "A tool result starting with '\u274c' is a typed error (invalid repo path, "
        "missing session, symbol/module not found, etc.). Read the message and follow "
        "its suggestion rather than retrying the same call unchanged.\n"
    ),
)

_registry = WorkspaceRegistry()

_FILE_TREE_DISPLAY_LIMIT = 200

P = ParamSpec("P")


def _tool_errors(operation: str) -> Callable[[Callable[P, str]], Callable[P, str]]:
    """Catch typed (and, as a last resort, unexpected) errors and format them.

    `ValueError` is handled the same as `CodewalkError`: the query layer
    (`codewalk.query.query`) intentionally raises plain `ValueError` for
    expected "not found" / invalid-input outcomes (unknown module, symbol,
    or file), and that behavior is pinned by existing tests -- so it isn't
    treated as an unexpected bug here, and doesn't get a noisy exception log.

    This is the one place broad `except Exception` is appropriate: it is the
    outermost MCP boundary, and a tool must never let a raw traceback escape
    through the protocol layer.
    """

    def decorator(fn: Callable[P, str]) -> Callable[P, str]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
            try:
                return fn(*args, **kwargs)
            except (CodewalkError, ValueError) as exc:
                return f"\u274c {operation} failed: {exc}"
            except Exception as exc:
                logger.exception("[%s] unexpected error", operation)
                return f"\u274c {operation} failed unexpectedly: {exc}"

        return wrapper

    return decorator


def _resolve(repo_path: str | None) -> Path:
    return resolve_repo_root(repo_path)


def _workspace(repo_path: str | None) -> tuple[Path, Workspace]:
    root = _resolve(repo_path)
    return root, _registry.get_or_build(root)


def _format_build_warnings(ws: Workspace) -> str:
    warnings = ws.last_build_warnings.all() if ws.last_build_warnings else []
    if not warnings:
        return ""
    shown = warnings[:20]
    lines = [f"\n\n**{len(warnings)} warning(s) during build:**"]
    lines.extend(f"- {w}" for w in shown)
    if len(warnings) > len(shown):
        lines.append(f"- ... and {len(warnings) - len(shown)} more")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
#  GRAPH / QUERY TOOLS
# ══════════════════════════════════════════════════════════════════


@mcp.tool()
@_tool_errors("codewalk_analyze_codebase")
def codewalk_analyze_codebase(repo_path: str | None = None, refresh: bool = False) -> str:
    """Build (or refresh) the dependency graph for a repo.

    Builds on first use; subsequent calls reopen the persisted graph without
    rescanning unless `refresh=True` forces a full rescan.

    Args:
        repo_path: Repo root. Defaults to the nearest `.git` ancestor of cwd.
        refresh: Force a full rescan even if a graph already exists on disk.
    """
    root = _resolve(repo_path)
    ws = _registry.refresh(root) if refresh else _registry.get_or_build(root)
    stats = ws.graph_store.get_stats()
    action = "Refreshed" if refresh else "Built"
    warnings_text = _format_build_warnings(ws)
    return (
        f"\u2705 {action} graph for `{root}`\n\n"
        f"- Files: {stats.files}\n"
        f"- Imports: {stats.imports}\n"
        f"- Symbols: {stats.symbols}\n"
        f"- Symbol calls: {stats.symbol_calls}\n"
        f"- Modules: {stats.modules}"
        f"{warnings_text}"
    )


@mcp.tool()
@_tool_errors("codewalk_refresh_analysis")
def codewalk_refresh_analysis(repo_path: str | None = None) -> str:
    """Force a full rescan and rebuild of the dependency graph for a repo."""
    root = _resolve(repo_path)
    ws = _registry.refresh(root)
    stats = ws.graph_store.get_stats()
    return (
        f"\u2705 Refreshed graph for `{root}`\n\n"
        f"- Files: {stats.files}\n- Imports: {stats.imports}\n"
        f"- Symbols: {stats.symbols}\n- Modules: {stats.modules}"
        f"{_format_build_warnings(ws)}"
    )


@mcp.tool()
@_tool_errors("codewalk_generate_config")
def codewalk_generate_config(repo_path: str | None = None, force: bool = False) -> str:
    """Write a starter `codewalk.yaml` at the repo root, unless one already exists.

    Args:
        repo_path: Repo root. Defaults to the nearest `.git` ancestor of cwd.
        force: Overwrite an existing `codewalk.yaml` if True.
    """
    root = _resolve(repo_path)
    path = generate_default_config(root, force=force)
    return f"\u2705 codewalk.yaml ready at `{path}`"


@mcp.tool()
@_tool_errors("codewalk_get_module_info")
def codewalk_get_module_info(module_name: str, repo_path: str | None = None) -> str:
    """Show a module's files, dependencies, and dependents."""
    _root, ws = _workspace(repo_path)
    return query.module_info_text(ws.graph_store, ws.graph_runtime, module_name)


@mcp.tool()
@_tool_errors("codewalk_explain_function")
def codewalk_explain_function(function_name: str, repo_path: str | None = None) -> str:
    """Explain a function: its source, callers, and callees."""
    root, ws = _workspace(repo_path)
    return query.explain_function_text(ws.graph_store, root, function_name)


@mcp.tool()
@_tool_errors("codewalk_explain_class")
def codewalk_explain_class(class_name: str, repo_path: str | None = None) -> str:
    """Explain a class: its source, members, callers, and callees."""
    root, ws = _workspace(repo_path)
    return query.explain_class_text(ws.graph_store, root, class_name)


@mcp.tool()
@_tool_errors("codewalk_lookup_symbol")
def codewalk_lookup_symbol(query_text: str, repo_path: str | None = None) -> str:
    """Look up a function, class, or method by name (or a close match)."""
    root, ws = _workspace(repo_path)
    return query.lookup_symbol_text(ws.graph_store, root, query_text)


@mcp.tool()
@_tool_errors("codewalk_get_overview")
def codewalk_get_overview(repo_path: str | None = None) -> str:
    """Give a high-level architecture overview: modules, entry points, dependency flow."""
    root, ws = _workspace(repo_path)
    overview = query.overview_text(ws.graph_store, ws.graph_runtime, root)
    stack = load_cached_stack_context(root)
    header = format_stack_context_header(stack) if stack else ""
    return f"{header}\n{overview}" if header else overview


@mcp.tool()
@_tool_errors("codewalk_get_blast_radius_map")
def codewalk_get_blast_radius_map(target: str = "", repo_path: str | None = None) -> str:
    """Show what would be affected by changing `target` (a file or module), or the
    top riskiest files/modules overall if `target` is empty."""
    _root, ws = _workspace(repo_path)
    return query.blast_radius_map_text(ws.graph_store, target)


@mcp.tool()
@_tool_errors("codewalk_find_circular_dependencies")
def codewalk_find_circular_dependencies(repo_path: str | None = None) -> str:
    """List circular import dependency cycles in the codebase, if any."""
    _root, ws = _workspace(repo_path)
    return query.find_circular_dependencies_text(ws.graph_runtime)


@mcp.tool()
@_tool_errors("codewalk_get_reading_order")
def codewalk_get_reading_order(module_name: str = "", repo_path: str | None = None) -> str:
    """Suggest a dependency-first file reading order (whole repo, or one module)."""
    _root, ws = _workspace(repo_path)
    return query.reading_order_text(ws.graph_store, module_name)


@mcp.tool()
@_tool_errors("codewalk_get_execution_flow")
def codewalk_get_execution_flow(module_name: str = "", repo_path: str | None = None) -> str:
    """Show module-to-module (or, given a module, file-to-file) dependency flow."""
    _root, ws = _workspace(repo_path)
    return query.execution_flow_text(ws.graph_store, module_name)


@mcp.tool()
@_tool_errors("codewalk_get_architecture_health")
def codewalk_get_architecture_health(repo_path: str | None = None) -> str:
    """Report architecture bottlenecks, cycles, and refactoring priorities."""
    root, ws = _workspace(repo_path)
    health = query.architecture_health_text(ws.graph_store, ws.graph_runtime)
    stack = load_cached_stack_context(root)
    header = format_stack_context_header(stack) if stack else ""
    return f"{header}\n{health}" if header else health


@mcp.tool()
@_tool_errors("codewalk_call_chain")
def codewalk_call_chain(source: str, target: str, repo_path: str | None = None) -> str:
    """Trace the shortest import path between two files."""
    _root, ws = _workspace(repo_path)
    return query.call_chain_text(ws.graph_store, ws.graph_runtime, source, target)


# ══════════════════════════════════════════════════════════════════
#  REVIEW TOOLS
# ══════════════════════════════════════════════════════════════════


def _diff_target_description(target_branch: str | None, staged: bool, commit: str | None) -> str:
    if commit:
        return f"commit `{commit}`"
    if staged:
        return "staged changes"
    if is_current_branch_alias(target_branch):
        return "current branch (staged + unstaged + untracked)"
    if target_branch:
        return f"working tree vs `{target_branch}`"
    return "local changes (staged + unstaged + untracked)"


def _require_review_target(
    root: Path, target_branch: str | None, staged: bool, commit: str | None
) -> str | None:
    """Return an ask-the-user prompt when the review base is unclear; else None."""
    if needs_review_target(target_branch, staged=staged, commit=commit):
        return format_ask_for_review_target(root)
    return None


def _format_review_start(
    heading: str, result: engine.ReviewStartResult, extra_lines: list[str] | None = None
) -> str:
    batch = result.first_batch
    assert result.session is not None and batch is not None
    lines = [
        f"# {heading}: `{result.session.session_id}`\n",
        f"- **{result.total_files} file(s)** in **{result.total_batches} batch(es)**",
        f"- Stack: {', '.join(result.stack.get('languages', []))} "
        f"+ {', '.join(result.stack.get('frameworks', []))}",
    ]
    if extra_lines:
        lines.extend(extra_lines)
    lines.append("")
    lines.append(f"## Batch 1/{result.total_batches}\n")
    lines.append(batch.context)
    lines.append("")
    lines.append("---")
    lines.append("**After reviewing this batch:**")
    lines.append(
        f"1. Call `codewalk_submit_batch_findings('{result.session.session_id}', "
        "findings=[...])` with your findings (empty list if clean)"
    )
    if result.total_batches > 1:
        lines.append(
            f"2. Call `codewalk_review_next_batch('{result.session.session_id}')` "
            "for the next batch"
        )
    else:
        lines.append(
            f"2. Call `codewalk_get_review_summary('{result.session.session_id}')` "
            "for the final verdict"
        )
    return "\n".join(lines)


@mcp.tool()
@_tool_errors("codewalk_run_review")
def codewalk_run_review(
    target_branch: str | None = None,
    staged: bool = False,
    commit: str | None = None,
    repo_path: str | None = None,
) -> str:
    """Start a batched code review and return the first batch's context.

    Requires an explicit review target: pass `target_branch="current"` for
    this branch's local work, or `target_branch="<base>"` to compare against
    that base (includes uncommitted changes). If omitted, returns a prompt so
    you can ask the user which branch to review against — never assume main.

    Call `codewalk_review_next_batch` after submitting findings for each batch.

    Always rebuilds the dependency graph from scratch before reviewing, so the
    review reflects the current on-disk state even if a stale graph was
    cached in memory from an earlier tool call in this session.
    """
    root = _resolve(repo_path)
    ask = _require_review_target(root, target_branch, staged, commit)
    if ask is not None:
        return ask
    workspace = _registry.refresh(root)
    result = engine.start_review(
        root, target_branch=target_branch, staged=staged, commit=commit, workspace=workspace
    )
    if not result.has_changes:
        target_desc = _diff_target_description(target_branch, staged, commit)
        return f"\u2705 No changes found to review ({target_desc})."
    return _format_review_start("Review Session", result)


@mcp.tool()
@_tool_errors("codewalk_re_review")
def codewalk_re_review(
    target_branch: str | None = None,
    staged: bool = False,
    commit: str | None = None,
    repo_path: str | None = None,
) -> str:
    """Start a fresh review session, hiding findings the user rejected last time.

    Same target rules as `codewalk_run_review`: require an explicit
    `target_branch` (or staged/commit). Always rebuilds the dependency graph
    from scratch before reviewing -- see `codewalk_run_review` for why.
    """
    root = _resolve(repo_path)
    ask = _require_review_target(root, target_branch, staged, commit)
    if ask is not None:
        return ask
    workspace = _registry.refresh(root)
    result = engine.re_review(
        root, target_branch=target_branch, staged=staged, commit=commit, workspace=workspace
    )
    if not result.has_changes:
        target_desc = _diff_target_description(target_branch, staged, commit)
        return f"\u2705 No changes found to re-review ({target_desc})."
    extra = (
        [f"- **{result.rejected_count} previously rejected finding(s)** will be hidden"]
        if result.rejected_count
        else None
    )
    return _format_review_start("Re-Review Session", result, extra_lines=extra)


@mcp.tool()
@_tool_errors("codewalk_review_next_batch")
def codewalk_review_next_batch(session_id: str, repo_path: str | None = None) -> str:
    """Get the next batch of files to review from an active review session."""
    root = _resolve(repo_path)
    batch = engine.next_batch(root, session_id)
    if batch is None:
        return (
            f"\u2705 All batches reviewed. Call "
            f"`codewalk_get_review_summary('{session_id}')` for the final verdict."
        )
    lines = [f"## Batch {batch.batch_index + 1}/{batch.total_batches}\n", batch.context, "", "---"]
    lines.append(
        f"1. Call `codewalk_submit_batch_findings('{session_id}', findings=[...])` "
        "with your findings"
    )
    if batch.batch_index + 1 < batch.total_batches:
        lines.append(f"2. Call `codewalk_review_next_batch('{session_id}')` for the next batch")
    else:
        lines.append(f"2. Call `codewalk_get_review_summary('{session_id}')` for the final verdict")
    return "\n".join(lines)


@mcp.tool()
@_tool_errors("codewalk_submit_batch_findings")
def codewalk_submit_batch_findings(
    session_id: str,
    findings: list[dict[str, Any]],
    notes: str = "",
    repo_path: str | None = None,
) -> str:
    """Save findings from the most recently reviewed batch.

    An empty `findings` list is valid -- it means "no issues found" in this
    batch. `notes` is optional context (e.g. why a batch is clean).

    Each finding dict:
      Required: severity ("blocker"|"error"|"suggestion"), category ("bug"|
        "security"|"style"|"test"|"blast_radius"|"design"|"naming"|
        "complexity"|"error_handling"|"type_safety"|"architecture"|
        "logging"|"privacy"|"hygiene"), file_path, title, explanation.
      Optional: line_number, current_code, recommended_code, blocking (bool,
        default False), subcategory.
    """
    root = _resolve(repo_path)
    result = engine.submit_findings(root, session_id, findings, notes=notes)
    return (
        f"\u2705 Saved {result.saved_count} finding(s) from batch {result.batch_number}. "
        f"Running total: {result.running_total}."
    )


def _format_finding_summary_line(idx: int, finding: Any) -> list[str]:
    blocking_tag = " \U0001f6ab BLOCKING" if finding.blocking else ""
    lines = [f"{idx}. **{finding.title}**{blocking_tag}"]
    location = finding.file_path
    if finding.line_number:
        location += f":{finding.line_number}"
    lines.append(f"   - File: `{location}`")
    lines.append(f"   - {finding.explanation}")
    return lines


def _format_review_coverage(summary: engine.ReviewSummary) -> list[str]:
    """Render the '### Review Coverage' section from `batch_outcomes`.

    Distinguishes batches that were actually submitted (with findings, or
    clean-and-justified) from ones the host silently skipped ("abandoned"),
    since `next_batch` doesn't gate on submission -- this is the only place
    that surfaces whether every batch was truly reviewed.
    """
    if not summary.batch_outcomes:
        return []

    outcomes = summary.batch_outcomes
    with_findings = sum(1 for o in outcomes.values() if o.get("outcome") == "findings")
    clean_justified = sum(1 for o in outcomes.values() if o.get("outcome") == "clean")
    not_reviewed = summary.total_batches - with_findings - clean_justified

    lines = [
        "",
        "### Review Coverage",
        f"- Batches with findings: **{with_findings}**",
        f"- Batches clean (justified): **{clean_justified}**",
    ]
    if not_reviewed > 0:
        lines.append(f"- Not reviewed (abandoned): **{not_reviewed}** \u26a0\ufe0f")

    clean_notes = [
        (batch_num, outcome.get("notes", ""))
        for batch_num, outcome in sorted(outcomes.items(), key=lambda item: int(item[0]))
        if outcome.get("outcome") == "clean" and outcome.get("notes")
    ]
    if clean_notes:
        lines.append("")
        lines.append("**Clean batch justifications:**")
        for batch_num, note in clean_notes[:20]:
            lines.append(f"- Batch {batch_num}: {note}")
        if len(clean_notes) > 20:
            lines.append(f"- ... and {len(clean_notes) - 20} more")
    lines.append("")
    return lines


@mcp.tool()
@_tool_errors("codewalk_get_review_summary")
def codewalk_get_review_summary(session_id: str, repo_path: str | None = None) -> str:
    """Get the combined static + LLM findings summary for a review session."""
    root = _resolve(repo_path)
    summary = engine.get_summary(root, session_id)

    lines = [f"# Review Summary \u2014 Session `{session_id}`\n"]
    lines.append(f"- **{summary.total_files} file(s)** in **{summary.total_batches} batch(es)**")
    lines.extend(_format_review_coverage(summary))

    total = len(summary.static_findings) + len(summary.llm_findings)
    blocking = sum(1 for f in summary.llm_findings if f.blocking)
    lines.append(
        f"- **{total} total finding(s)** "
        f"({len(summary.static_findings)} architectural + {len(summary.llm_findings)} from review)"
    )
    if summary.rejected_filtered_count:
        lines.append(
            f"- **{summary.rejected_filtered_count} previously rejected finding(s)** hidden"
        )
    if blocking:
        lines.append(f"- **{blocking} BLOCKING** (must fix before merge)")
    lines.append("")

    if summary.static_findings:
        lines.append("## Architectural Warnings (deterministic)\n")
        for i, f in enumerate(summary.static_findings, 1):
            lines.extend(_format_finding_summary_line(i, f))
            lines.append("")

    if summary.llm_findings:
        lines.append("## Review Findings\n")
        for severity in ("blocker", "error", "suggestion"):
            group = [f for f in summary.llm_findings if f.severity.value == severity]
            if not group:
                continue
            lines.append(f"### {severity.upper()} ({len(group)})\n")
            for i, f in enumerate(group, 1):
                lines.extend(_format_finding_summary_line(i, f))
                lines.append("")

    lines.append("---")
    lines.append(
        "**Produce your final verdict:** BLOCKING findings \u2192 `request_changes`; "
        "otherwise `approve` with comments."
    )
    lines.append(
        "Present findings to the user; edit `llm_findings.json` to set `user_verdict` to "
        "'accepted'/'rejected' per finding, then call "
        f"`codewalk_accept_and_verify_fix('{session_id}')`."
    )
    return "\n".join(lines)


@mcp.tool()
@_tool_errors("codewalk_get_review_details")
def codewalk_get_review_details(session_id: str, repo_path: str | None = None) -> str:
    """Retrieve a persisted review session's metadata for introspection/debugging."""
    root = _resolve(repo_path)
    details = engine.get_review_details(root, session_id)
    session = details.session
    lines = [
        f"# Review Session Details \u2014 `{session.session_id}`\n",
        f"- Status: {session.status.value}",
        f"- Repo: `{session.repo_path}`",
        f"- Branch: `{session.current_branch}` \u2192 `{session.target_branch or 'working tree'}`",
        f"- Created: {session.created_at}",
        f"- Static findings: {details.static_findings_count}",
        f"- LLM findings: {details.llm_findings_count}",
    ]
    if details.batch_state:
        lines.append(
            f"- Batches: {details.batch_state.current_batch_index + 1}/"
            f"{details.batch_state.total_batches} returned so far"
        )
    return "\n".join(lines)


@mcp.tool()
@_tool_errors("codewalk_get_stack_info")
def codewalk_get_stack_info(repo_path: str | None = None) -> str:
    """Return the repo's file tree plus a prompt for the host LLM to detect the tech stack.

    No diff, no static analysis -- just the file tree. Call
    `codewalk_save_stack_context` with the resulting JSON afterward.
    """
    root = _resolve(repo_path)
    config = load_codewalk_yaml(root)
    scan_result = scan_repo(root, config)
    file_paths = sorted(f.file_path for f in scan_result.files)

    tree_text = "\n".join(f"- {p}" for p in file_paths[:_FILE_TREE_DISPLAY_LIMIT])
    if len(file_paths) > _FILE_TREE_DISPLAY_LIMIT:
        tree_text += f"\n- ... and {len(file_paths) - _FILE_TREE_DISPLAY_LIMIT} more files"

    prompt = STACK_DETECT_PROMPT.format(
        available_rubrics=", ".join(sorted(AVAILABLE_RUBRICS)),
        file_tree=tree_text,
        changed_files="(not applicable -- detecting overall project stack)",
    )
    return (
        f"{prompt}\n\n---\n\n"
        "**After analyzing the above**, respond with the JSON object and call "
        "`codewalk_save_stack_context(your_json)` to save it."
    )


@mcp.tool()
@_tool_errors("codewalk_save_stack_context")
def codewalk_save_stack_context(stack: dict[str, Any], repo_path: str | None = None) -> str:
    """Persist the host-authored stack context JSON to `.codewalk/stack_context.json`."""
    root = _resolve(repo_path)
    cleaned = save_stack_context(root, stack)
    languages = ", ".join(cleaned.get("languages", []))
    frameworks = ", ".join(cleaned.get("frameworks", []))
    return f"\u2705 Saved stack context: {languages} + {frameworks}"


def _format_command_result(heading: str, result: exec_tools.CommandResult) -> list[str]:
    if result.skipped_reason:
        return [f"### {heading}", f"_Skipped: {result.skipped_reason}_", ""]
    status = "\u2705 PASSED" if result.ok else "\u274c FAILED"
    lines = [f"### {heading} \u2014 {status}", f"Command: `{result.command}`", ""]
    if result.stdout:
        lines.extend(["```", result.stdout, "```"])
    if result.stderr:
        lines.extend(["**stderr:**", "```", result.stderr, "```"])
    lines.append("")
    return lines


@mcp.tool()
@_tool_errors("codewalk_run_static_analysis")
def codewalk_run_static_analysis(file_paths: list[str], repo_path: str | None = None) -> str:
    """Run language-aware static analyzers (e.g. ruff, go vet) on the given files."""
    root = _resolve(repo_path)
    for file_path in file_paths:
        resolve_within_repo(root, file_path)
    config = load_codewalk_yaml(root)
    results = exec_tools.run_static_analysis(root, file_paths, config)
    if not results:
        return f"\u2139\ufe0f No static-analysis command configured for {len(file_paths)} file(s)."

    lines = [f"## Static Analysis \u2014 {len(file_paths)} file(s)\n"]
    for result in results:
        lines.extend(_format_command_result(result.command, result))
    return "\n".join(lines)


@mcp.tool()
@_tool_errors("codewalk_run_tests")
def codewalk_run_tests(file_paths: list[str] | None = None, repo_path: str | None = None) -> str:
    """Run the project's test suite (language auto-detected from `file_paths`)."""
    root = _resolve(repo_path)
    for file_path in file_paths or []:
        resolve_within_repo(root, file_path)
    config = load_codewalk_yaml(root)
    result = exec_tools.run_tests(root, file_paths or [], config)
    if result is None:
        return "\u2139\ufe0f No test command configured for the detected language."
    return "\n".join(_format_command_result("Test run", result))


@mcp.tool()
@_tool_errors("codewalk_accept_and_verify_fix")
def codewalk_accept_and_verify_fix(session_id: str, repo_path: str | None = None) -> str:
    """Return user-accepted findings for the host to apply and verify itself.

    Reads `llm_findings.json` and returns only findings with
    `user_verdict == "accepted"`. Never edits any file -- the host applies
    fixes with its own editing tools, then verifies with
    `codewalk_run_static_analysis` + `codewalk_run_tests`.
    """
    root = _resolve(repo_path)
    summary = engine.get_summary(root, session_id)
    findings = summary.llm_findings

    if not findings:
        return f"\u274c No findings in session `{session_id}`. Run a review first."

    accepted = [f for f in findings if f.user_verdict == "accepted"]
    rejected = sum(1 for f in findings if f.user_verdict == "rejected")
    undecided = len(findings) - len(accepted) - rejected

    if not accepted:
        return (
            "\u26a0\ufe0f No accepted findings. Edit `llm_findings.json` and set "
            "`user_verdict` to 'accepted' for the issues to fix, then call this tool again."
        )

    lines = [
        f"## Accepted Fixes \u2014 Session `{session_id}`\n",
        f"The user accepted **{len(accepted)} finding(s)**. Apply these fixes with your own "
        "editing tools, then verify with `codewalk_run_static_analysis` and "
        "`codewalk_run_tests` on the modified files.\n",
    ]
    for idx, finding in enumerate(accepted, 1):
        location = finding.file_path
        if finding.line_number:
            location += f":{finding.line_number}"
        lines.append(f"### #{idx} {finding.title}")
        lines.append(f"- Location: `{location}`")
        lines.append(f"- Severity: {finding.severity.value}")
        lines.append(f"- Issue: {finding.explanation}")
        if finding.current_code:
            lines.extend(["- Current code:", "```", finding.current_code, "```"])
        if finding.recommended_code:
            lines.extend(["- Recommended code:", "```", finding.recommended_code, "```"])
        lines.append("")

    skipped_parts = []
    if rejected:
        skipped_parts.append(f"{rejected} rejected")
    if undecided:
        skipped_parts.append(f"{undecided} undecided")
    if skipped_parts:
        lines.append(f"({', '.join(skipped_parts)} \u2014 skipped)")

    return "\n".join(lines)


install_github_staleness_wrappers(mcp._tool_manager)


def main() -> None:
    """Entry point for `python -m codewalk.mcp.server` (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
