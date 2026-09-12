# Architecture

fver is four commands over five packages. It never modifies user files: all
state lives in `<repo>/.fver/`.

| Package | What | Depends on the proof system? |
|---|---|---|
| `core/` | `models.py` the vocabulary (Target, TranslationUnit, FunctionInfo, Claim, Finding, Status, Cost); `config.py` the one config file; `workspace.py` the `.fver/` layout and the write guard; `context.py` `AppContext.load()` = workspace + config + ledger + backend + detected target | no |
| `index/` | `sources.py` which .c files and how to read each (own dir + every header dir, no macros); `preprocess.py`; `functions.py` tree-sitter extraction; `callgraph.py`; `attack_surface.py` the ranking; `targets.py` ABI detection; `scan.py` the pipeline, ending with the backend front-end saying which functions it accepts | via the Backend interface |
| `ledger/` | `api.py` the `Ledger` protocol, `sqlite.py` the implementation (WAL, concurrent workers), `memory.py` for tests, `cache.py` cache keys | no |
| `prove/` | `select.py` chooses and orders functions (callees first); `hunt.py` + `cbmc.py` run CBMC first; `loop.py` the propose, check, repair loop with guardrails and budget; `client.py` the Anthropic client (and the scripted fake); `prompts.py`, `parse.py`, `store.py` (proof files and cache), `invalidate.py` (staleness), `retrieval.py` (examples), `pricing.py` | via the Backend interface |
| `backends/` | `base.py` the `Backend` interface; `refinedc/` the one real backend (annotation grammar, opaque floats, output parsing, guardrails, audit); `null.py` for tests | yes |
| `commands/` | `setup`, `init`, `prove`, `status`: thin typer wrappers | via AppContext |

```
fver setup  : package manager + opam, pinned commits; tools run from the switch
fver init   : .fver/, config.toml with the API key slot, .gitignore
fver prove  : index if sources changed -> select -> CBMC -> loop -> ledger + proofs/
fver status : index if sources changed -> ledger -> one page
```

## Per-function proofs

The unit is the function. Its proof depends on its body hash, the contracts
of its callees, the trusted specs it uses and the tool versions; that tuple
is the cache key. When a body or a callee contract changes, `invalidate.py`
marks the dependent proofs stale and `fver prove` redoes them. Functions the
front-end rejects are recorded `unsupported` with the reason and never sent
to the prover.

## Guardrails

The LLM may add `[[rc::...]]` annotations and Rocq lemmas; it may not change
the C. `backend.guardrail` compares the submission with the original function
token by token before the checker runs, and rejects `Admitted`, new axioms
and `rc::trust_me`. Every accepted proof is audited with `Print Assumptions`;
the assumptions land in the claim.

## CBMC first

`prove/hunt.py` runs CBMC over the selected functions. A finding with a
real `main` as entry point is high confidence and records a `bug_found`
claim; harness-driven findings (no `main`) are low confidence and only
informational. Functions with a confirmed bug are not sent to the prover.

## Floats

RefinedC has no floating point. `backends/refinedc/opaque.py` rewrites
`float`/`double`/`long double` to same-size structs in the copy the checker
sees (and in shadow copies of the project's headers), so layouts are
unchanged and functions that only move floats verify; arithmetic on them is
reported unsupported.

## Logs

One rotating `.fver/logs/fver.log`, every line tagged with the command.
LLM transcripts go under `.fver/logs/llm/<run>/`.
