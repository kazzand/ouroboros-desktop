# Pre-Commit Review Checklists

Single source of truth for all automated review checklists (Bible P5: DRY).
Loaded by `ouroboros/tools/review.py` at review time and injected into the
multi-model review prompt.

When a new reviewable concern appears, add it here — not in prompts or docs.

### Review-exempt operations

The following tools create commits but are **exempt** from multi-model review
(Bible P8 explicit exception):

- `restore_to_head` — discards uncommitted changes (not a commit, no review needed)
- `revert_commit` — creates a mechanical inverse of an already-reviewed commit

Rationale: review gates on rollbacks create a paradox where reviewers block
the undo for missing tests/VERSION, trapping the agent with broken code.
These tools restore to already-reviewed states by definition.

---

## Repo Commit Checklist

Used by `repo_commit` and `repo_write_commit` for all changes to the
Ouroboros repository.

| # | item | what to check | severity when FAIL |
|---|------|---------------|--------------------|
| 1 | bible_compliance | Does the diff violate any BIBLE.md principle? | critical |
| 2 | development_compliance | Does it follow DEVELOPMENT.md patterns? Check explicitly: (a) naming conventions (snake_case modules/vars, PascalCase classes, UPPER_SNAKE_CASE constants); (b) entity type rules — Gateway classes contain ONLY transport, no business logic; Tool functions are thin wrappers; (c) module size ≤ 1000 lines, methods ≤ 150 lines, ≤ 8 params; (d) no gratuitous abstract layers (P5 Minimalism); (e) new LLM calls go through the shared `LLMClient`/`llm.py` layer, not ad-hoc HTTP clients; (f) cognitive artifacts (identity.md, scratchpad, task reflections, review outputs) must NOT use hardcoded `[:N]` truncation — explicit omission notes required; (g) new `get_tools()` exports follow the ToolEntry pattern in registry.py. | critical |
| 3 | secrets_check | Are secrets, API keys, .env files, credentials present in the diff? | critical |
| 4 | code_quality | Careful code review: bugs, logic errors, crashes, regressions, race conditions, resource leaks? | critical |
| 5 | security_issues | Security vulnerabilities: injection, path traversal, secret leakage, unsafe operations? | critical |
| 6 | tests_affected | Did code logic change without corresponding test changes? (PASS if only docs/config/memory changed, or if tests are present.) | critical |
| 7 | architecture_doc | New module, endpoint, or data flow added but ARCHITECTURE.md not updated? (Write "Not applicable" with PASS if no architectural change.) | critical |
| 8 | version_bump | Behavior changed but VERSION not updated? (PASS if no behavior change.) | critical |
| 9 | changelog_and_badge | VERSION bumped but README.md badge or changelog not updated? (PASS if VERSION not bumped.) | critical |
| 10 | tool_registration | New tool function added but not exported in get_tools()? (PASS if no new tool.) | critical |
| 11 | context_building | New data/memory files that should appear in LLM context (context.py) but don't? | advisory |
| 12 | knowledge_index | Knowledge base topics changed but memory/knowledge/index-full.md not updated? | advisory |
| 13 | self_consistency | Does this change affect behavior described in `BIBLE.md`, `prompts/`, `docs/`, or this checklist itself? Check explicitly: (a) version in `ARCHITECTURE.md` header matches `VERSION` file; (b) tool names/descriptions in `prompts/SYSTEM.md` match tools actually exported by `get_tools()`; (c) JSONL log/memory file formats described in `ARCHITECTURE.md` match all readers/writers; (d) any behavioral change reflected in `prompts/CONSCIOUSNESS.md` if it affects background loop behavior; (e) DEVELOPMENT.md rules still accurate after the change. | critical |
| 14 | cross_platform | Does the diff use platform-specific APIs (`os.kill`, `os.setsid`, `os.killpg`, `os.getpgid`, `fcntl`, `msvcrt`, `signal.SIGKILL`, `signal.SIGTERM`, `subprocess` with `start_new_session`/`creationflags`, hardcoded `/` or `\\` in filesystem paths) outside of `ouroboros/platform_layer.py`? Does it import Unix-only or Windows-only modules (`fcntl`, `msvcrt`, `winreg`, `resource`) at any level without a platform guard (`sys.platform`/`IS_WINDOWS` check)? | critical |

### Severity rules

- Items 1-5 are always critical.
- Items 6-10, 14 are conditionally critical: FAIL only when the condition applies.
  If the condition does not apply, write verdict PASS with a short reason
  (e.g. "Not applicable — no code logic change").
- Items 11-12 are advisory: FAIL produces a warning but does not block.
- Item 13 (self_consistency) is conditionally critical: FAIL only when a
  concrete stale artifact is identified (specific file, line, or symbol).
  If no concrete staleness is found, write verdict PASS with a short reason.

---

## Intent / Scope Review Checklist

Used by the supplemental blocking scope reviewer.
This reviewer checks completeness and forgotten touchpoints using richer context
than the diff-only triad.

| # | item | what to check | severity when FAIL |
|---|------|---------------|--------------------|
| 1 | intent_alignment | Does the staged change actually fulfill the intended transformation, not merely touch related files? | critical if the incompleteness is concrete and evidenced; otherwise advisory |
| 2 | forgotten_touchpoints | Are there specific coupled files, tests, prompts, docs, configs, or sibling paths that must also change? Name the exact file(s) or symbol(s). | critical if a required touchpoint is concretely omitted; otherwise advisory |
| 3 | cross_surface_consistency | If behavior changed, are adjacent surfaces still consistent: prompts, docs, comments, tool descriptions, automation, or user-visible workflow? | critical if a concrete stale surface leaves the repo internally inconsistent; otherwise advisory |
| 4 | regression_surface | Does wider repository context show a concrete sibling path, migration edge, or parallel flow that remains broken or incomplete after this change? | critical if it leaves a concrete broken/incomplete path; otherwise advisory |
| 5 | prompt_doc_sync | If prompts or docs are relevant to the changed behavior, are they still accurate and mutually consistent? | critical if a concrete prompt/doc artifact becomes false or stale; otherwise advisory |
| 6 | architecture_fit | Does the change solve the class of problem, or is it a narrow patch that leaves the underlying pattern unresolved? | advisory |

### Severity rules

- Any critical FAIL must cite a concrete file, symbol, prompt, doc, test, config, or sibling flow.
- If the reviewer cannot point to an exact touchpoint, the FAIL must be advisory, not critical.
- Scope affects only unchanged code outside the diff. The diff itself remains fully reviewable.
