# fver architecture

fver is a CLI you run inside a C repository. It proves, function by
function, that the code cannot exhibit undefined behaviour, using an LLM to
write annotations/proofs and a proof checker to accept or reject them. It
never modifies user files: all state lives in `<repo>/.fver/`.

## Module map (src/fver/)

| Module | Owns | Knows about backend? |
|---|---|---|
| `core/models.py` | Domain vocabulary: Target, TranslationUnit, FunctionInfo, Claim, Finding, Status, Cost | no |
| `core/config.py` | `.fver/config.toml` schema (pydantic), loading, saving. Run-shaping defaults live here (`[verify]`, `[budget]`, `[model]`, `[hunters]`); CLI flags are one-run overrides and default to None so the config wins | no (opaque `[backend.<name>]` table) |
| `core/workspace.py` | `.fver/` layout, write guard (refuses to write outside `.fver/`) | no |
| `core/context.py` | `AppContext.load()` = workspace + config + ledger + backend | constructs it via registry only |
| `build/` | Build capture: `detect.py` infers the build at scan time (compile_commands.json, `bear -- make`, CMake/Meson export dirs, fallback flags) unless `[build]` names a source of flags; per-TU preprocessing; target detection | no |
| `extract/` | Function extraction (tree-sitter), call graph, attack-surface ranking | no |
| `ledger/api.py` | `Ledger` protocol; `ledger/sqlite.py` implements it; `ledger/cache.py` cache keys; `ledger/report.py` | no |
| `backends/base.py` | The `Backend` protocol + Submission/CheckResult/FunctionTask/PromptContext | this IS the boundary |
| `backends/registry.py` | Discovery via entry points (`fver.backends` group) + built-ins | by name only |
| `backends/refinedc/` | RefinedC on Rocq. Annotation splicing, `refinedc check` invocation, output parsing, guardrails, prompt reference | yes |
| `backends/refinedc/opaque.py` | Opaque floating point: rewrites `float`/`double`/`long double` to same-size structs in the TU copy and in shadow copies of project headers (`<workspace>/shadow/`, placed before the real include dirs); generates `fver_opaque.h`. Float-computing functions become unsupported; everything else in the file stays checkable | yes |
| `backends/refinedc/shims/` | Header shims for the Cerberus front-end: POSIX headers it lacks (`-I` after project dirs) and `setjmp.h` (force-included under Cerberus's own guard because Cerberus's copy ends in `#error`) | yes |
| `backends/null.py` | A fake backend for tests and pipeline dry runs | yes (trivially) |
| `hunters/` | Bug finders: CBMC always; sanitizers over the project's tests when `hunters.test_command` is set. Produce `Finding`s | no |
| `agent/` | Anthropic client, prompt assembly, the propose -> check -> repair loop, retrieval of examples, budget, cheating detection | only via `Backend` |
| `agent/protocol.py` | The agent protocol: task packaging, check, next, changed as dicts; shared by CLI and MCP | only via `Backend` |
| `mcp/server.py` | MCP server over the protocol (`fver mcp`) | no |
| `agent/invalidate.py` | Stale-proof tracking: explicit STALE claims when a body, callee contract, external spec or tool version changed; caller invalidation after a contract change | only via `Backend` |
| `commands/` | One module per subcommand, each with `register(app)` | via AppContext |
| `commands/setup.py` | `fver setup`: installs every external tool (package manager for bear/cbmc/opam, then an opam switch `fver` with Rocq, Iris, Cerberus and RefinedC at pinned commits) and records the binary paths in the user config. Nothing is optional: missing tools are errors, never silently skipped | no |
| `commands/docs.py` | `fver docs` and the `.fver/GUIDE.md` that `fver init` writes: layout, command reference generated from the Typer app, config reference from the pydantic defaults, prover workflow (mirrored by `.claude/skills/fver/SKILL.md`, test-enforced) | no |
| `cli.py` | Typer app; imports command modules | no |

## Data flow

```
fver scan   : build/ -> extract/ -> ledger (TUs, functions) -> backend.translate -> ledger (unsupported)
fver hunt   : hunters/ -> ledger (findings, BUG_FOUND claims)
fver verify : ledger (next functions) -> agent.loop(backend) -> proofs/ + ledger (claims)
fver status : ledger.summary
```

## Proof lifecycle

A claim's status is derived, not stored: a VERIFIED claim whose `body_hash`
differs from the function's current hash reads as STALE (`ledger/memory.py:
derive_status`). `fver scan` then calls `agent/invalidate.reconcile`, which
records explicit STALE claims (with reasons) and also catches cache-key drift
from callee contracts, external specs and tool versions. When the loop
re-verifies a function with a different contract, it marks every verified
caller STALE immediately. A caller proven while its callee is stale carries
the assumption `callee-contract-unverified:<name>` until the callee is
re-verified.

## Verification order

`fver verify` ranks by attack score, then pulls each selected function's
not-yet-verified internal callees in front of it (post-order over the call
graph, cycles cut at the first repeat), so a caller is attempted only once
its callees have contracts. `--no-deps` disables this. External callees
(libc, other libraries) are never pulled in; they need trusted specs under
`.fver/external/`.

## Bug-hunter semantics

There are two hunters: CBMC, which always runs, and the sanitizers, which rebuild and run the project's own tests and therefore need `hunters.test_command`. Hunters produce `Finding`s with a `confidence`. CBMC run from a real `main`
yields `high`; CBMC run per function with unconstrained inputs yields `low`
(the reported violation may be a precondition every caller satisfies). Only
high-confidence findings of a UB kind become `bug_found` claims. Each hunt
run replaces that hunter's previous findings for the files it covered, and a
function whose earlier hunter-issued `bug_found` is not reproduced gets a
superseding claim. `missing_body`, `bound_reached` and `tool_error` are informational kinds.

## Rescans

`fver scan` prunes translation units and functions that are no longer in
the build (their claim and finding history stays). Build captures are kept
under `.fver/work/compile_commands.json`; a capture that compiled nothing
(build already up to date) reuses the previous good one so function ids stay
stable across scans.

## External provers (session mode)

`agent/protocol.py` exposes the loop's building blocks as functions returning
dicts: `reference`, `task`, `check`, `next_functions`, `changed`, `status`,
`show`, `scan`, `hunt`. `Verifier.attempt_submission` is the single
guardrail -> check -> audit -> save step used by both the API loop and
`Verifier.submit` (external prover; zero LLM cost, `extra["prover"] =
"external"`). `commands/agent_cmds.py` (`fver task|check|next|changed`) and
`mcp/server.py` (`fver mcp`, stdio, optional dependency `fver[mcp]`) are
thin wrappers over the protocol, so a Claude Code session, a script and the
built-in loop are judged identically. `.claude/skills/fver/SKILL.md` is the
workflow for a session.

## Offline trials

`FVER_FAKE_LLM=<json file with a list of scripted responses> fver verify`
substitutes a scripted client so the whole pipeline can be exercised with the
`null` backend and no API calls. Never use it for real verification.

## Contracts subagents must respect

- Only `backends/<name>/` may import that backend's tools. The agent loop
  and commands use `Backend` methods exclusively.
- Only `ledger/sqlite.py` imports sqlite3.
- Anything that writes files goes through `Workspace` paths, and calls
  `ws.assert_not_user_file(path)` if the path came from outside.
- Every LLM call goes through `agent/client.py` (cost accounting, caching,
  refusal handling, retries live there).
- `FunctionInfo.body_hash` is computed once in `extract/` and is the basis
  of the cache key in `ledger/cache.py`.
- Accepted submissions are stored under `.fver/proofs/<source path>/<fn>/`
  as the submission files plus `result.json` (CheckResult fields + cache key).
- Tests use the `null` backend and never require external tools or network.

## Adding a backend

Implement `fver.backends.base.Backend`, register it under the
`fver.backends` entry-point group (or add to `_BUILTIN` in registry.py),
and put its settings under `[backend.<name>]` in config.toml. Nothing else
changes.
