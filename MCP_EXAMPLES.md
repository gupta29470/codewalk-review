# MCP Tool Examples

One example prompt per tool — what you'd naturally ask your MCP host (Copilot,
Claude, Cursor, ...), and which tool call it maps to. All tools accept an
optional `repo_path` argument (defaults to the nearest `.git` ancestor of the
server's working directory), omitted below for brevity.

## Setup

**`codewalk_analyze_codebase(refresh?)`**
> "Analyze this codebase."
Builds the dependency graph on first use. Add `refresh=True` after major
structural changes to force a full rescan instead of reopening the cached graph.

**`codewalk_refresh_analysis()`**
> "The code changed a lot since we last indexed — rebuild the graph."
Always forces a full rescan and rebuild, regardless of what's cached.

**`codewalk_generate_config(force?)`**
> "Set up a codewalk.yaml for this project."
Detects the repo's tech stack and writes a starter `codewalk.yaml` with
stack-specific excludes layered on top of the core safety-net excludes.

## Query

**`codewalk_get_module_info(module_name)`**
> "What's in the `analysis` module?"
Files, languages, dependencies, dependents, hub files, coupling stats.

**`codewalk_explain_function(function_name)`**
> "What does `get_blast_radius` do?"
Source location + body, plus which files break if it changes.

**`codewalk_explain_class(class_name)`**
> "Explain the `GraphStore` class."
Same as `explain_function`, routed for classes/components/types.

**`codewalk_lookup_symbol(query_text)`**
> "Where is `resolve_within_repo` defined?"
Deterministic name lookup with fuzzy fallback if there's no exact match.

**`codewalk_get_overview()`**
> "Give me an overview of this repo."
Modules, dependency flow, riskiest files by blast radius. Shows a "Declared
Architecture" header if stack context has been saved.

**`codewalk_get_blast_radius_map(target?)`**
> "What breaks if I change `scanner.py`?"
Pass a file name, a module name, or leave empty for the top 30 riskiest files
repo-wide.

**`codewalk_find_circular_dependencies()`**
> "Are there any circular imports in this codebase?"
Strongly-connected cycle groups plus suggested edges to break.

**`codewalk_get_reading_order(module_name?)`**
> "Where should I start reading this codebase?"
All files (or one module's files) in dependency order, dependencies first.

**`codewalk_get_execution_flow(module_name?)`**
> "How do the modules depend on each other?"
Module-to-module flow with no argument; file-to-file flow inside a module if
`module_name` is given.

## Architecture

**`codewalk_get_architecture_health()`**
> "How healthy is this architecture? What should I refactor first?"
Bottleneck files (betweenness), key files (PageRank), circular dependencies
with suggested fixes.

**`codewalk_call_chain(source, target)`**
> "How does `server.py` end up depending on `config.py`?"
Shortest import path between two files.

## Stack context (optional, enriches overview + review)

**`codewalk_get_stack_info()`**
> "Detect this project's tech stack."
Returns the file tree plus a prompt for your agent to analyze and describe
the stack as JSON.

**`codewalk_save_stack_context(stack)`**
> (Your agent calls this automatically after analyzing `get_stack_info`'s output.)
Persists the JSON to `.codewalk/stack_context.json` — set up once per repo,
survives across commits.

## Review

**`codewalk_run_review(target_branch?, staged?, commit?)`**
> "Review my changes."
Starts a batched review session over local changes (staged + unstaged +
untracked by default) and returns the first batch's context.

**`codewalk_submit_batch_findings(session_id, findings, notes?)`**
> (Your agent calls this after reviewing each batch.)
Persists findings for the batch just reviewed. An empty `findings` list is
valid — it means the batch was clean.

**`codewalk_review_next_batch(session_id)`**
> (Your agent calls this to move to the next batch.)
Returns context for the next batch, or a completion message once all batches
are done.

**`codewalk_get_review_summary(session_id)`**
> "Summarize the review findings."
Combined architectural + review findings, batch coverage stats, and verdict
guidance.

**`codewalk_get_review_details(session_id)`**
> "What's the status of that review session?"
Session metadata: status, branch, finding counts, batches returned so far.

**`codewalk_re_review(target_branch?, staged?, commit?)`**
> "I've addressed the feedback — review it again."
Starts a fresh session, hiding any finding you previously rejected.

**`codewalk_accept_and_verify_fix(session_id)`**
> "Apply the fixes I accepted."
Returns only the accepted findings (after you've set `user_verdict` in
`llm_findings.json`) for your agent to apply with its own editing tools.

## Maintenance

**`codewalk_run_static_analysis(file_paths)`**
> "Run the linter on the files I just changed."
Runs the language-appropriate linter/type-checker configured in
`codewalk.yaml` (e.g. `ruff`, `go vet`).

**`codewalk_run_tests(file_paths?)`**
> "Run the tests."
Runs the project's test suite; `file_paths` helps auto-detect which
language's test command to use.
