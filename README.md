# fver

**Prove that C code cannot do the dangerous things C allows.**

fver is a command-line tool you run inside a C repository. Function by
function it establishes, with a machine-checked proof, that the code cannot
read or write out of bounds, use freed memory, dereference null, overflow a
signed integer, or shift out of range. An LLM writes the annotations; a
proof checker decides whether they are right. The default checker is
Frama-C with WP and RTE (ACSL contracts, SMT solvers); RefinedC on Rocq is
available as a foundational alternative. fver keeps the books: what is
proven, what is not, and why.

fver never edits your source files. Everything it produces lives in
`.fver/` at the root of your repository.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/TheoOltean/fver/main/get-fver.sh | sh
```

Linux or macOS. This installs `uv` if missing, puts `fver` in
`~/.local/bin`, then runs `fver setup`, which installs everything else:
`cbmc`, and an opam switch holding Frama-C with WP and Alt-Ergo, and Rocq,
Iris, Cerberus and RefinedC pinned to the commits fver is tested against.
The first run builds the toolchain and takes 30 to 60 minutes; rerunning
resumes where it stopped.
System packages go through brew, apt-get, dnf or pacman. A C compiler must
already be present.

From a checkout: `./install.sh`.

## Use

```sh
cd your-c-repo
fver init                    # creates .fver/; put your API key in .fver/config.toml
fver status                  # indexes the code, then: per file, what is provable and proven
fver prove                   # prove the whole repository, highest attack surface first
fver prove src/lzio.c        # one file
fver prove luaZ_fill         # one function (its unproven callees come first)
fver status src/lzio.c       # every function in the file with its state
fver status luaZ_fill        # one function: contract, history, cost, what depends on it
```

That is the whole interface. `fver prove` indexes the code if it changed,
runs CBMC over the selected functions first so a function with a concrete
bug is recorded instead of sent to the prover, then proves the rest in
dependency order under the budget in the config, printing one line per
function as it finishes.

`fver status` is a plain page like `git status`: one line per file with
counts of verified, unresolved, bug, waiting and unsupported functions, and
a note when a file cannot be read. A function is **verified** when the
checker accepted a proof, **unresolved** when the prover ran out of attempts
or money (rerunning continues), **bug** when CBMC found a concrete
undefined-behaviour trace, **waiting** when nothing has been tried, and
**unsupported** when the proof checker's C front-end rejects it, with the
reason.

## Configuration

One file, `.fver/config.toml`, ignored by git because it holds the API key.
`fver init` writes the settings you are expected to touch, with every other
key and its default in a comment above:

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

## What gets proven

Only properties defined by the C language itself, so no one has to write a
specification of what the program is supposed to do and the LLM cannot pass
by proving something trivial:

| Property | Stops |
|---|---|
| no out-of-bounds read or write | buffer overflows |
| no use after free, no double free | heap exploits |
| no null / dangling dereference, no pointer arithmetic out of its object | crashes |
| no signed overflow, division by zero, bad shifts | length-check bypasses |

(RefinedC additionally proves no read of uninitialised memory; WP does not
track initialisation.)

The proof for a function depends only on that function's body and the
contracts of the functions it calls. Editing a function invalidates one
proof; changing a contract invalidates its callers too; everything else
stays proven.

## Two checkers

**Frama-C/WP** (default, `project.backend = "framac"`): the RTE plugin turns
every potential undefined behaviour into an assertion, WP generates proof
obligations from the ACSL contract the model writes, and SMT solvers
discharge them. Frama-C parses essentially all C and ships ACSL
specifications for the C library, so calls to `memcpy`, `strlen` or `malloc`
need nothing from the project. The trusted base is Frama-C, WP and the
solvers.

**RefinedC** (`project.backend = "refinedc"`): ownership types checked in
Rocq, the smallest trusted base available, at the cost of a front-end that
rejects char and string literals, casts in constant expressions, unions
copied by value, varargs, float arithmetic and unsigned wraparound.
`fver status <file>` reports the reason per function.

## Trust

- The proof checker is the oracle. The LLM cannot make anything verified;
  only the checker can.
- Generated proofs may not use escape hatches (`Admitted`, new axioms,
  `rc::trust_me`). Every accepted proof is audited with `Print Assumptions`
  and its assumption set is stored in the ledger.
- The LLM may add annotations to a function but may not change its code.
  fver checks this mechanically before the checker ever runs.
- Specs for external functions (libc) are trusted and listed as such.

## Layout of `.fver/`

```
config.toml      configuration, including the API key (ignored)
ledger.sqlite    what is proven, by whom, at what cost (commit this)
proofs/          accepted submissions mirroring your source tree (commit this)
external/        trusted specs for external functions (commit this)
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
