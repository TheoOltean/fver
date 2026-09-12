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

One line, no sudo for fver itself, on Linux or macOS. It installs `uv` if
missing, puts `fver` in `~/.local/bin`, then runs `fver setup`, which
installs everything else fver needs: `cbmc`, and an opam switch
holding Rocq, Iris, Cerberus and RefinedC, pinned to the commits fver is
tested against. The first run builds the proof toolchain and takes 20 to 40
minutes; rerunning resumes where it stopped.

```sh
curl -fsSL https://raw.githubusercontent.com/TheoOltean/fver/main/get-fver.sh | sh
```

System packages go through your package manager (brew, apt-get, dnf or
pacman, which may ask for sudo). A C compiler must already be present
(`xcode-select --install` on macOS, `build-essential` on Debian/Ubuntu).
There are no optional tools: `fver setup` ends with a table that is either
all green or tells you what is missing.

From a local checkout instead:

```sh
./install.sh                 # uv tool install --editable, then fver setup
```

For development:

```sh
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q                                  # no toolchain needed
FVER_REFINEDC_BIN=~/.opam/fver/bin/refinedc .venv/bin/python -m pytest -q tests/integration
```

To pin a release instead of `main`, set `FVER_REF` (a tag or commit) before
piping to `sh`, e.g. `FVER_REF=v0.1.0`. To upgrade, run the one-liner again.

Credentials: fver calls the Anthropic API. After `fver init`, put the key in
the project's `.fver/config.toml`, which git ignores:

```sh
fver config set model.api_key sk-ant-...
```

Alternatively export `ANTHROPIC_API_KEY`, or run `ant auth login` once and
the SDK will pick up the profile. `fver setup` shows which source is used.

## Use

```sh
cd your-c-repo
fver init                  # once: .fver/ with a short commented config and GUIDE.md
fver status                # index the code; what is provable, proven, and why not
fver prove                 # the whole repository, highest attack surface first, under the budget
fver prove src/lzio.c      # one file
fver prove luaZ_fill       # one function (its unproven callees come first)
fver status luaZ_fill      # one function: contract, attempts, cost, who depends on it
```

`fver prove` does everything in order: it indexes the code if it has never
been indexed or sources changed, runs CBMC over the selected functions so a
function with a concrete bug is recorded instead of sent to the prover, then
proves the rest in dependency order. On a terminal it shows a live view: a
tree of directories, files and functions coloured by status, details of the
selected function on the right, coverage and cost at the bottom. Off a
terminal (CI, a pipe) it prints one line per function.

`fver status` opens the same view read-only, indexing the code first if
needed; off a terminal it prints a table, or one function's details when
given a name.

The one config is `.fver/config.toml`, ignored by git: the API key, the
model, its effort, and the budget per run and per function, each with a
comment. `fver config set <key> <value>` changes a setting, `fver config`
prints all of them with their effective values, and `.fver/GUIDE.md`
documents every one. Everything else has a default.

## Two ways to run the prover

**API mode.** `fver prove` calls the Anthropic API itself: fully
autonomous, parallel, budgeted per function and per run, results cached.
Needs the API key in the config. Best for sweeping a whole codebase.

**Session mode.** A Claude Code session (or any MCP client, or a script,
or you) acts as the prover. fver keeps the parts that must not be left to
the model: packaging the task, the guardrails, the proof checker, the
audit and the ledger. No API key; the cost is your Claude subscription;
it is interactive and works while you edit code. Best for day-to-day
development and for functions the autonomous loop could not close.

Both modes record into the same ledger; `fver status <fn>` says which prover
produced a proof.

## Use from Claude Code

Register the MCP server once (from anywhere; it opens the `.fver/` of the
repository the session is in):

```sh
claude mcp add fver -- fver mcp
```

or put this in the repository's `.mcp.json`:

```json
{
  "mcpServers": {
    "fver": { "command": "fver", "args": ["mcp"] }
  }
}
```

Tools: `fver_reference`, `fver_next`, `fver_task`, `fver_check`,
`fver_changed`, `fver_status`, `fver_show`, `fver_scan`, `fver_hunt`. A
skill describing the workflow ships in `.claude/skills/fver/SKILL.md`; copy
it into your repository's `.claude/skills/` (or `~/.claude/skills/`) so
`/fver` is available in any session.

The same protocol is available as plain commands, so a session without MCP
can drive it through the shell:

```sh
fver agent next                                   # what to prove, callees first
fver agent task luaZ_read > .fver/scratch/task.md # the packet: reference, code, contracts
fver agent check luaZ_read --submission .fver/scratch/luaZ_read.c   # exit 0 = verified
fver agent changed                                # after edits: which proofs went stale
```

To have proofs checked as you edit, add a cheap hook to `.claude/settings.json`
(it re-indexes without running the backend; keep it that way):

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "case \"$(jq -r .tool_input.file_path)\" in *.c|*.h) fver agent changed;; esac"
          }
        ]
      }
    ]
  }
}
```

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
echo '["```c file=function.c\n/* FVER_ACCEPT */\nint f(void){return 0;}\n```"]' > .fver/fake.json
FVER_FAKE_LLM=.fver/fake.json fver prove
```

The `null` backend accepts any submission containing `FVER_ACCEPT`; the
fake client replays scripted responses. This exercises indexing, the loop,
the ledger, caching and staleness tracking with no external dependencies.

## What the checker cannot see

RefinedC's semantics has no floating point, and its front-end rejects a
whole file as soon as a struct or union declaration contains a `float` or
`double`. fver checks a copy of the code in which floating-point types are
replaced by same-size structs without arithmetic (`struct fver_f64` and
friends), and mirrors project headers with the same rewrite. A function
that only stores, copies or passes float values can be verified; one that
adds, compares or converts them is reported `unsupported` with that reason.
This is sound for undefined-behaviour proofs because IEEE arithmetic has no
undefined behaviour of its own and the memory layout is unchanged; what is
lost is coverage of the functions that compute with floats. Switch it off
with `backend.refinedc.opaque_floats = false`.

Also unsupported per function: variadic functions, `setjmp`/`longjmp`
callers (the include itself is fine), copying a whole union value, and
reading or writing union members of a union that is not annotated at its
definition. `fver status` reports every one of these per function.

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
config.toml      configuration, including the API key (ignored)
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
