# fver

fver is a command line utility that proves C code free of undefined
behaviour: no out-of-bounds reads or writes, no use after free, no null
dereference, no signed overflow, no bad shifts. If you're a macOS or Linux
user, just open up a shell and paste in the following curl line:

```sh
curl -fsSL https://raw.githubusercontent.com/TheoOltean/fver/main/get-fver.sh | sh
```

It installs `fver` into `~/.local/bin` and then the proof toolchain
(CBMC, Frama-C with WP and Alt-Ergo, and RefinedC on Rocq). The first
install builds the toolchain and takes a while; rerunning resumes where it
stopped.

Now, you can navigate to a specific project and initialize fver in it:

```sh
cd your-c-repo
fver init
```

This creates a `.fver/` directory at the root of the repository. That is
the only place fver ever writes: your source files are never touched. It
holds one config file (ignored by git, because your API key goes in it),
the ledger of what is proven, and the proofs themselves.

From here, you can set your API key in `.fver/config.toml` and get to work.
Literally just point at a file or a function:

```sh
fver prove src/parse.c
fver prove parse_hex4
```

or do a full sweep with:

```sh
fver prove
```

fver will read the code (no build system needed), run CBMC over it first
to catch concrete bugs, and then generate full proofs function by function,
callees before callers, under the money cap in the config. For each
function the model writes a contract and loop annotations, the proof
checker judges them, and its feedback goes back to the model until the
proof goes through or the attempts run out. The model can never mark
anything verified; only the checker can, and the code it checks is
compared token by token with yours before it ever runs.

Once your proofs are completed, you can see what sections of code are safe
and unsafe using:

```sh
fver status                  # one line per file, like git status
fver status src/parse.c      # every function in the file, with the reason if not proven
fver status parse_hex4       # one function: its contract, history, cost, what depends on it
```

A function is **verified** when the checker accepted a proof, **unresolved**
when the prover ran out of attempts or money or is waiting on a callee's
contract (rerunning continues), **bug** when CBMC found a concrete
undefined-behaviour trace, **waiting** when nothing has been tried, and
**unsupported** when the checker cannot represent it, with the reason.

Currently fver uses Frama-C with the WP plugin as its default checker:
the model writes ACSL contracts, Frama-C turns every potential undefined
behaviour into a proof obligation, and SMT solvers discharge them. RefinedC
on Rocq is available as a second, foundational checker with a much smaller
trusted base, but its front-end rejects a lot of ordinary C. These
technologies are still being developed, and so is fver: the checker sits
behind one interface, and what we want next is a foundational checker with
a complete representation of C, whether that comes from RefinedC's
front-end catching up or from the Lean-based C semantics now appearing.

## Configuration

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

## Trust

- The checker is the oracle. The model cannot make anything verified.
- The model may add annotations to a function; it may not change its code.
- Escape hatches (`admit`, `axiom`, `assumes`, `requires \false` in ACSL;
  `Admitted`, new axioms, `rc::trust_me` in RefinedC) are rejected before
  checking, and every proof's assumptions are stored with it.
- A proof is conditional on the function's contract: `requires` clauses are
  promises the callers make, and fver proves each caller keeps them. Specs
  for the C library are trusted and recorded as such.

## Layout of `.fver/`

```
config.toml      configuration, including the API key (ignored)
ledger.sqlite    what is proven, by whom, at what cost (commit this)
proofs/          accepted proofs and every attempt's feedback (commit this)
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
