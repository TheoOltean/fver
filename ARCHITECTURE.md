# Architecture

fver is four commands over five packages. It never modifies user files: all
state lives in `<repo>/.fver/`.

| Package | What | Depends on the proof system? |
|---|---|---|
| `core/` | `models.py` the vocabulary (Target, TranslationUnit, FunctionInfo, Claim, Finding, Status, Cost); `config.py` the one config file; `workspace.py` the `.fver/` layout and the write guard; `context.py` `AppContext.load()` = workspace + config + ledger + backend + detected target | no |
| `index/` | `sources.py` which `.c` files and how each is read (own dir + every header dir, no macros); `preprocess.py`; `functions.py` tree-sitter extraction; `callgraph.py`; `attack_surface.py` the ranking; `targets.py` ABI detection; `scan.py` the pipeline, ending with the backend front-end saying which functions it accepts, propagated to their callers | via the Backend interface |
| `ledger/` | `api.py` the `Ledger` protocol, `sqlite.py` the implementation (WAL, concurrent workers), `memory.py` for tests, `cache.py` cache keys | no |
| `prove/` | `select.py` chooses and orders functions (callees first); `hunt.py` + `cbmc.py` run CBMC first; `loop.py` the propose, check, repair loop with guardrails, budget and dependency-aware scheduling; `client.py` the Anthropic client (and the scripted fake); `prompts.py`, `parse.py`, `store.py` (proof files and cache), `invalidate.py` (staleness), `retrieval.py` (examples), `pricing.py` | via the Backend interface |
| `backends/` | `base.py` the `Backend` interface; `framac/` the default (ACSL helpers, WP output parsing, guardrail, prompts); `refinedc/` the foundational one (annotation grammar, opaque floats, output parsing, audit, prompts); `null.py` for tests | yes |
| `commands/` | `setup`, `init`, `prove`, `status`: thin typer wrappers | via AppContext |

```
fver setup  : package manager + opam, pinned commits; tools run from the switch
fver init   : .fver/, config.toml with the API key slot, .gitignore
fver prove  : index if sources changed -> select -> CBMC -> loop -> ledger + proofs/
fver status : index if sources changed -> ledger -> one page
```

## The Backend interface

A backend knows one proof system and nothing else knows any. It answers
six questions: which functions of a file can it represent (`translate`),
what must the model produce (`submission_spec`, `prompt_context`), is this
submission allowed at all (`guardrail`), does it prove the function
(`check`), what did the proof assume (`audit`), and what part of an accepted
submission do callers depend on (`extract_spec`).

`check` writes one C file into the backend's workspace: the original source
with the target replaced by the annotated submission, every other function
definition reduced to a prototype, and each callee's accepted contract
attached to its prototype. So the checker sees exactly the target's body
plus contracts, and an unsupported construct elsewhere in the file cannot
sink the target.

**Frama-C/WP** runs `frama-c -wp -wp-rte -wp-fct <fn> -wp-print` and reads
the goal statuses and the printed unproved goals back into feedback with
the source line and the property WP could not prove. Termination goals are
excluded. **RefinedC** runs `refinedc check`, maps Rocq locations back to
source lines, and after acceptance runs `Print Assumptions`.

## Per-function proofs and dependencies

The unit is the function. Its proof depends on its body hash, the contracts
of its callees, the trusted specs it uses and the tool versions; that tuple
is the cache key. When a body or a callee contract changes, `invalidate.py`
marks the dependent proofs stale and `fver prove` redoes them.

A caller cannot be checked without its callees' contracts, so:

- at index time, a function whose callee the front-end rejects is itself
  unsupported, transitively;
- at run time, a function waits for callees that are part of the same run,
  is recorded `blocked` without calling the model when a callee ended
  without a contract, and stops after the attempt in which the checker
  first reports a missing contract (a call hidden behind a macro).

## Guardrails

The model may add annotations (ACSL comments for Frama-C; `[[rc::...]]`
attributes and Rocq lemmas for RefinedC); it may not change the C.
`backend.guardrail` rejects escape hatches by pattern and `check` compares
the submission with the original function token by token, comments
stripped, before the checker runs.

## CBMC first

`prove/hunt.py` runs CBMC over the selected functions. A finding from a
real `main` is high confidence and records a `bug_found` claim; without a
`main` CBMC runs briefly on the two highest-ranked functions per file and
its findings are informational only.

## Floats under RefinedC

RefinedC has no floating point. `backends/refinedc/opaque.py` rewrites
`float`/`double`/`long double` to same-size structs in the copy the checker
sees, so layouts are unchanged and functions that only move floats verify;
arithmetic on them is reported unsupported.

## Logs

One rotating `.fver/logs/fver.log`, every line tagged with the command.
LLM transcripts go under `.fver/logs/llm/<run>/`; every attempt's
submission and feedback under `.fver/proofs/<file>/<function>/attempts/`.
