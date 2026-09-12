# fver

**Prove that C code cannot do the dangerous things C allows.**

fver is a command-line tool you run inside a C repository. Function by
function it establishes, with a machine-checked proof, that the code cannot
read or write out of bounds, use freed memory, dereference null, overflow a
signed integer, or shift out of range. An LLM writes the annotations; a
proof checker decides whether they are right; fver keeps the books. It
never edits your source files: everything it produces lives in `.fver/` at
the root of your repository.

## How it works

1. **Index.** fver reads every `.c` file directly, with its own directory
   and every header directory in the repository on the include path. No
   build system is involved. It extracts the functions, the call graph and
   a rough attack-surface score (pointer-and-length parameters, loops over
   arrays, externally reachable), and asks the checker's front-end which
   functions it can represent.
2. **Look for bugs first.** CBMC, a bounded model checker, runs over the
   selected functions. A concrete undefined-behaviour trace from a real
   entry point is recorded as a bug and that function is not sent to the
   prover.
3. **Prove.** For each function, callees before callers, the model is given
   the function, its file, the contracts of the functions it calls and the
   checker's rules, and asked for annotations. The checker judges the
   result; its feedback goes back to the model; up to N attempts within a
   money cap per function and per run. The model can never mark anything
   verified. Only the checker can.
4. **Keep the books.** A SQLite ledger records every claim with the
   function's body hash, the callee contracts it relied on and the tool
   versions. Edit a function and its proof goes stale; change a contract
   and its callers go stale; everything else stays proven.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/TheoOltean/fver/main/get-fver.sh | sh
```

Linux or macOS. This installs `uv` if missing, puts `fver` in
`~/.local/bin`, then runs `fver setup`, which installs everything else
through your package manager (brew, apt-get, dnf or pacman) and opam:
`cbmc`, Frama-C with WP and Alt-Ergo, and Rocq, Iris, Cerberus and RefinedC
pinned to the commits fver is tested against. The first run builds the
toolchain and takes 30 to 60 minutes; rerunning resumes where it stopped.
A C compiler must already be present. From a checkout: `./install.sh`.

## Use

```sh
cd your-c-repo
fver init                    # creates .fver/; put your API key in .fver/config.toml
fver status                  # indexes the code; per file, what is provable and proven
fver prove                   # prove the whole repository, highest attack surface first
fver prove src/parse.c       # one file
fver prove parse_hex4        # one function (its unproven callees come first)
fver status src/parse.c      # every function in the file with its state and reason
fver status parse_hex4       # one function: contract, history, cost, what depends on it
```

That is the whole interface. `fver prove` prints one line per function as
it finishes; `fver status` is a plain page like `git status`. A function is

- **verified**: the checker accepted a proof;
- **unresolved**: the prover ran out of attempts or money, or a callee it
  needs has no contract yet (`blocked: ...`); rerunning continues;
- **bug**: CBMC found a concrete undefined-behaviour trace;
- **waiting**: nothing has been tried;
- **unsupported**: the checker's front-end rejects it, or it calls a
  function the front-end rejects, with the reason.

## Configuration

One file, `.fver/config.toml`, ignored by git because it holds the API key.
`fver init` writes the settings you are expected to touch; every other key
and its default is in a comment above them.

```toml
[model]
api_key = "sk-ant-..."     # or leave empty and export ANTHROPIC_API_KEY
workspace_id = ""          # only if the API says the key is not scoped to a workspace
model = "claude-fable-5-1"
effort = "high"            # low | medium | high | xhigh | max

[budget]
max_usd_per_run = 200.0
max_usd_per_function = 10.0
```

The run cap stops new functions from being scheduled; the ones in flight
finish, so a run can end a few dollars over it.

## What gets proven

Only properties defined by the C language itself, so no one has to write a
specification of what the program is supposed to do and the model cannot
pass by proving something trivial:

| Property | Stops |
|---|---|
| no out-of-bounds read or write | buffer overflows |
| no use after free, no double free | heap exploits |
| no null or dangling dereference, no pointer arithmetic out of its object | crashes |
| no signed overflow, division by zero, bad shifts | length-check bypasses |

A proof is conditional on the function's contract: `requires` clauses are
promises the callers make, and fver proves each caller keeps them. Specs
for the C library are trusted and recorded as assumptions on every proof
that uses them.

## Two checkers

**Frama-C/WP** (default, `project.backend = "framac"`). The model writes
ACSL: a `/*@ requires ... assigns ... ensures ... */` contract and loop
invariants. Frama-C's RTE plugin turns every potential undefined behaviour
in the function into an assertion, WP generates the proof obligations, and
SMT solvers discharge them. Frama-C parses essentially all C and ships
ACSL specifications for the C library, so `memcpy`, `strlen` and `malloc`
need nothing from the project. Termination is not required. The trusted
base is Frama-C, WP and the solvers. WP does not track initialisation, so
reads of uninitialised memory are outside its guarantee.

**RefinedC** (`project.backend = "refinedc"`). Ownership types checked in
Rocq: the smallest trusted base available, and it also rules out reads of
uninitialised memory. Its front-end rejects char and string literals, casts
in constant expressions, unions copied by value, varargs, float arithmetic
and unsigned wraparound, which in practice excludes most of an ordinary
codebase; `fver status <file>` reports the reason per function.

## Trust

- The checker is the oracle. The model cannot make anything verified.
- The model may add annotations to a function; it may not change its code.
  fver compares the submission with the original token by token before the
  checker ever runs.
- Escape hatches are rejected before checking: for ACSL `admit`, `axiom`,
  `axiomatic`, `assumes`, ghost code and `requires \false`; for RefinedC
  `Admitted`, new axioms and `rc::trust_me`, and every accepted Rocq proof
  is audited with `Print Assumptions`.
- Every proof's assumptions (tool versions, library specs, callee
  contracts) are stored in the ledger and shown by `fver status <function>`.

## Layout of `.fver/`

```
config.toml      configuration, including the API key (ignored)
ledger.sqlite    what is proven, by whom, at what cost (commit this)
proofs/          accepted submissions and every attempt's feedback (commit this)
external/        trusted specs for external functions, if you write any (commit this)
work/ backend/ cache/ logs/   derived state (ignored)
```

## Development

```sh
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q                                  # no toolchain needed
FVER_REFINEDC_BIN=~/.opam/fver/bin/refinedc .venv/bin/python -m pytest -q tests/integration
```

The `null` backend (`fver init --backend null`) accepts any submission
containing `FVER_ACCEPT`, and `FVER_FAKE_LLM=<json list of replies>` replays
scripted responses, so the whole pipeline runs without tools or credentials.
See `ARCHITECTURE.md` for the layout of the code.
