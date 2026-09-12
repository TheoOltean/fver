# fver architecture

fver is a CLI you run inside a C repository. It proves, function by
function, that the code cannot exhibit undefined behaviour, using an LLM to
write annotations and a proof checker (RefinedC on Rocq) to accept or reject
them. It never modifies user files: all state lives in `<repo>/.fver/`.

## Packages (src/fver/)

| Package | Owns | Knows the backend? |
|---|---|---|
| `core/` | `models.py` domain vocabulary (Target, TranslationUnit, FunctionInfo, Claim, Finding, Status, Cost); `config.py` the `.fver/config.toml` schema and the commented writer; `workspace.py` the `.fver/` layout and the write guard; `context.py` `AppContext.load()` = workspace + config + ledger + backend + detected target; `doctor.py` tool checks; `guide.py` the generated `GUIDE.md` | constructs it via the registry only |
| `index/` | Everything that turns a repository into indexed functions: `detect.py` decides how to read the tree (an exported compile_commands.json if present, else the source directly with every header directory on the include path), `compile_commands.py` parses or synthesises the per-file commands, `preprocess.py`, `targets.py` detects the ABI, `functions.py` extracts functions (tree-sitter), `callgraph.py`, `attack_surface.py` ranks them, `scan.py` runs the whole index and asks the backend what it can represent | no |
| `prove/` | The proof pipeline: `select.py` chooses and orders functions (callees first), `hunt.py` + `cbmc.py` run CBMC over a selection, `loop.py` the propose -> check -> repair loop (`Verifier`), `client.py` the Anthropic client (cost, caching, refusals, transcripts), `prompts.py`, `retrieval.py` few-shot examples from accepted proofs, `store.py` saved submissions, `invalidate.py` staleness, `protocol.py` the session-mode protocol (task, check, next, changed, status, show) shared by the CLI and MCP | only via `Backend` |
| `backends/` | `base.py` the `Backend` protocol (the boundary), `registry.py` (two built-ins), `null.py` for tests, `refinedc/` the real one: front-end driving, annotation splicing, guardrails, audit, `opaque.py` opaque floats, `shims/`, `prompts/` | this IS the boundary |
| `ledger/` | `api.py` the `Ledger` protocol, `sqlite.py` the implementation (WAL, concurrent workers), `memory.py` for tests, `cache.py` cache keys, `report.py` markdown/JSON export | no |
| `mcp/` | `server.py`: the protocol over stdio for Claude Code | no |
| `tui.py` | The Textual view behind `fver status` and `fver prove` | no |
| `commands/` | Thin typer wrappers: `setup`, `init`, `prove`, `status`, `config`, `mcp`, and the hidden `agent` group (task, check, next, changed) | via AppContext |

## Data flow

```
fver prove  : index/scan (if stale) -> prove/hunt (CBMC) -> prove/select -> prove/loop(backend) -> proofs/ + ledger
fver status : ledger -> tui (or a table / markdown / JSON off a terminal)
fver agent  : prove/protocol -> the same loop pieces, one step at a time, for a session
```

## Proof lifecycle

A claim's status is derived, not stored: a VERIFIED claim whose `body_hash`
differs from the function's current hash reads as STALE (`ledger/memory.py:
derive_status`). Indexing then calls `prove/invalidate.reconcile`, which
records explicit STALE claims (with reasons) and also catches cache-key drift
from callee contracts, external specs and tool versions. When the loop
re-verifies a function with a different contract, it marks every verified
caller STALE immediately. A caller proven while its callee is stale carries
the assumption `callee-contract-unverified:<name>` until the callee is
re-verified.

## Order of work

`fver prove` ranks by attack score, then pulls each selected function's
not-yet-verified internal callees in front of it (post-order over the call
graph, cycles cut at the first repeat), so a caller is attempted only once
its callees have contracts. External callees (libc, other libraries) are
never pulled in; they need trusted specs under `.fver/external/`. Naming a
function explicitly also retries one an earlier run left unresolved.

## CBMC before the prover

`prove/hunt.py` runs CBMC over the selected functions first. Run from a real
`main`, a hit is a real bug (`high` confidence) and the function is recorded
`bug_found` and not sent to the prover; run per function with unconstrained
inputs, a hit is `low` confidence (the reported violation may be a
precondition every caller satisfies) and is recorded without changing the
status. `missing_body`, `bound_reached` and `tool_error` are informational.

## Rescans

Indexing prunes translation units and functions that are no longer in the
build (their claim and finding history stays). Build captures are kept under
`.fver/work/compile_commands.json`; a capture that compiled nothing (build
already up to date) reuses the previous good one so function ids stay stable.
The detected target is cached in `.fver/work/target.json`.

## Session mode

`prove/protocol.py` exposes the loop's building blocks as functions returning
dicts. `Verifier.attempt_submission` is the single guardrail -> check -> audit
-> save step used by both the API loop and external submissions (zero LLM
cost, `extra["prover"] = "external"`). `fver agent ...` and `fver mcp` are
thin wrappers over it, so a Claude Code session, a script and the built-in
loop are judged identically. `.claude/skills/fver/SKILL.md` is the workflow
for a session and mirrors the text in `core/guide.py` (test-enforced).

## Offline trials

`FVER_FAKE_LLM=<json file with a list of scripted responses> fver prove`
substitutes a scripted client so the whole pipeline can be exercised with the
`null` backend and no API calls. Never use it for real verification.

## Contracts

- Only `backends/refinedc/` may import RefinedC's tools. The loop and the
  commands use `Backend` methods exclusively.
- Only `ledger/sqlite.py` imports sqlite3.
- Anything that writes files goes through `Workspace` paths, and calls
  `ws.assert_not_user_file(path)` if the path came from outside.
- Every LLM call goes through `prove/client.py`.
- `FunctionInfo.body_hash` is computed once in `index/` and is the basis of
  the cache key in `ledger/cache.py`.
- Accepted submissions are stored under `.fver/proofs/<source path>/<fn>/`
  as the submission files plus `result.json` (CheckResult fields + cache key).
- Tests use the `null` backend and never require external tools or network;
  `tests/integration/` runs against a real RefinedC when `FVER_REFINEDC_BIN`
  is set.
- Nothing is optional: a missing tool is an error (`fver setup` installs
  everything), never a silently degraded result.
