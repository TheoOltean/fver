# fver

**Prove that C code cannot do the dangerous things C allows.**

fver is a command-line tool you run inside a C repository. It works function
by function to establish, with a machine-checked proof, that the code cannot
read or write out of bounds, use freed memory, dereference null, overflow a
signed integer, or read uninitialised memory. An LLM writes the annotations
and proofs; a proof checker (RefinedC on Rocq) decides whether they are
right. fver keeps the books: what is proven, what is not, under which
assumptions, and at what cost.

fver never edits your source files. Everything it produces lives in
`.fver/` at the root of your repository.

## Install

One line, no sudo, on any Unix-like system (Linux, macOS, BSD). Needs
Python 3.11+ and curl or wget; installs `uv` if it is missing and puts
`fver` in `~/.local/bin`:

```sh
curl -fsSL https://raw.githubusercontent.com/TheoOltean/fver/main/get-fver.sh | sh
```

From a local checkout instead:

```sh
./install.sh                 # uv tool install --editable, or pipx
```

For development:

```sh
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

External tools (checked by `fver doctor`): a C compiler, `bear` or CMake
for build capture, `cbmc` and `cerberus` for bug hunting, and for the
RefinedC backend `opam`, Rocq, dune and `refinedc`. fver runs without any
of them, but with reduced function.

To pin a release instead of `main`, set `FVER_REF` (a tag or commit) before
piping to `sh`, e.g. `FVER_REF=v0.1.0`. To upgrade, run the one-liner again.

Credentials and model: fver calls the Anthropic API. Put the key in your
user-level config (never committed) and pick the model there too:

```sh
fver config set --user model.api_key sk-ant-...
fver config set --user model.model claude-fable-5-1     # default
fver config set --user model.effort high                # low | medium | high | xhigh | max
```

Alternatively export `ANTHROPIC_API_KEY`, or run `ant auth login` once and
the SDK will pick up the profile. `fver doctor` shows which source is used.

## Use

```sh
cd your-c-repo
fver init          # creates .fver/config.toml; detects your build system and target
fver doctor        # what tools and credentials are available
fver scan          # capture the build, index every function, run the backend front-end
fver hunt          # run CBMC / Cerberus / sanitizers; concrete bugs go straight into the ledger
fver verify        # let the LLM prove functions, highest attack surface first
fver status        # what is proven
fver show parse_header
fver report        # full markdown report under .fver/reports/
```

`fver verify` options worth knowing: `--function NAME`, `--file PATH`,
`--limit N`, `--max-usd X`, `--dry-run`, `--retry-unresolved`, `--recheck`.

## What gets proven

Only properties defined by the C language itself, so no one has to write a
specification of what the program is supposed to do and the LLM cannot pass
by proving something trivial:

| Property | Stops |
|---|---|
| no out-of-bounds read or write | buffer overflows |
| no use after free, no double free | heap exploits |
| no null / dangling / misaligned dereference | crashes |
| no signed overflow, division by zero, bad shifts | length-check bypasses |
| no read of uninitialised memory | information leaks |

The proof for a function depends only on that function's body and the
contracts of the functions it calls. Editing a function invalidates one
proof; changing a contract invalidates its callers too; everything else
stays proven. Results are cached by content hash so a commit re-verifies
only what changed.

## Trying it without tools or credentials

```sh
fver init --backend null
fver scan
echo '["```c file=function.c\n/* FVER_ACCEPT */\nint f(void){return 0;}\n```"]' > .fver/fake.json
FVER_FAKE_LLM=.fver/fake.json fver verify
```

The `null` backend accepts any submission containing `FVER_ACCEPT`; the
fake client replays scripted responses. This exercises scan, the loop, the
ledger, caching and staleness tracking with no external dependencies.

## Trust

- The proof checker is the oracle. The LLM cannot make anything "verified";
  only the checker can.
- Generated proofs may not use escape hatches (`Admitted`, new axioms,
  `rc::trust_me`). Every accepted proof is audited with `Print Assumptions`
  and its assumption set is stored in the ledger.
- The LLM may add annotations to a function but may not change its code.
  fver checks this mechanically before the checker ever runs.
- Specs for external functions (libc, syscalls) are trusted and listed as
  such in every report.

## Layout of `.fver/`

```
config.toml      configuration (commit this)
ledger.sqlite    what is proven, by whom, at what cost (commit this)
proofs/          accepted submissions mirroring your source tree (commit this)
external/        trusted specs for external functions (commit this)
work/ backend/ cache/ logs/   derived state (ignored)
```

## Architecture

See `ARCHITECTURE.md`. The proof system is behind one interface
(`fver.backends.base.Backend`); everything else is independent of it.
A `null` backend exists for tests and dry runs. See `PLAN.md` for the
reasoning behind the design.
