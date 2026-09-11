# Making C safe with LLM-written Rocq proofs — plan

> Status (2026-09-12): the harness described here exists as the `fver` CLI
> in this repository. See README.md for usage and ARCHITECTURE.md for the
> code layout. Sections 1-7 below are the design it implements; section 8's
> experiment is the next step and needs RefinedC installed.

## 1. Goal

Take a C codebase and mechanically prove that it cannot do any of the
things that make C dangerous: overrun a buffer, use freed memory, read
uninitialised memory, overflow a signed integer, dereference null, and so
on. Every one of these is defined by the C language itself, not by what the
program is *supposed* to do. So nobody has to write a specification of what
the program does, an LLM cannot "cheat" by proving something trivial, and
the bugs eliminated are the ones behind most memory-corruption CVEs.

Functional correctness, secrecy properties and anything needing human-
written intent are out of scope now. The chosen stack (Rocq) can express
them later, and the safety proofs become the foundation for that.

## 2. The checklist

What "safe" means. Each item is a class of undefined behaviour (UB). A
function is *safe* when all of them are proven impossible for every input
its callers can give it.

| # | Property | What it stops |
|---|----------|---------------|
| 1 | No out-of-bounds read or write | buffer overflows |
| 2 | No use after free, no double free | heap exploits |
| 3 | No null / dangling / misaligned dereference | crashes, some exploits |
| 4 | No signed overflow, division by zero, bad shifts | length-check bypasses |
| 5 | No read of uninitialised memory | info leaks, nondeterminism |
| 6 | No invalid pointer arithmetic or comparison | leads to #1 |
| 7 | No data races (later; needs concurrency support) | TOCTOU, corruption |
| 8 | No memory leak (optional; a resource property, not UB) | denial of service |

Implementation-defined choices (int width, char signedness, alignment)
are fixed per build target and recorded with every result.

## 3. The stack

**Rocq** (formerly Coq) is the proof assistant. Every proof is checked by
its small kernel. This is the single source of truth and the single
storage format for proofs.

**RefinedC** is the C verification library inside Rocq. It provides:

- a mathematical definition of what C programs mean (Caesium), written to
  match the ISO standard, including UB, uninitialised memory and pointer
  provenance;
- the **Cerberus** front-end, which parses real C, handles the
  implementation-defined choices per target, and translates each function
  into a Rocq term;
- a type system of "refinement types" that describe what memory a function
  owns and what facts hold about its values, written as annotations in the
  C source;
- **Lithium**, an automation engine that takes the annotated function and
  tries to prove it type-checks (which is exactly UB-freedom), reducing
  the work to a set of leftover pure facts about integers, lists and sets.

**CBMC** and the **Cerberus interpreter** run underneath as free bug finders:
they never prove anything but they turn up real UB from the test suite
before any proof effort is spent, and a definite bug is a ledger entry too.

**No SMT solver sits in the trusted path.** RefinedC's leftover facts are
closed by Rocq's own decision procedures (`lia`, `nia` for arithmetic,
`set_solver` for sets) or by hand-written tactics. An SMT solver can be
used as an *untrusted oracle*: tools like CoqHammer and SMTCoq call Z3 or
cvc5, then reconstruct a proof the Rocq kernel checks. If the
reconstruction fails the goal is simply not closed. So SMT is optional
speed, never trust.

## 4. What gets written, and in what language

Three kinds of text, in decreasing order of volume:

1. **Annotations in the C file** (RefinedC's type language). For each
   function: what each pointer argument owns (`&own<array<int<i32>, n>>`
   means "an owned array of n 32-bit ints"), what must hold on entry
   (`n ≤ max_int i32`), what is returned, and for each loop, which
   variables change and what stays true. The expressions inside are Rocq
   terms, so this is already "Rocq embedded in C comments". For simple
   code this is the *only* thing written; Lithium does the rest.

2. **Rocq tactics for leftover goals.** When Lithium reduces a function to
   pure facts it cannot close automatically, those become ordinary Rocq
   goals about integers, lists and sets, with no C in them. They are closed
   with tactic scripts, either inline via an annotation or as helper lemmas
   in a companion `.v` file. This is standard Rocq, and it is the part
   where LLMs have the most training data.

3. **Custom types and predicates in Rocq.** When the built-in types are not
   enough (a linked list, a ring buffer, a hash table with invariants),
   someone defines a new refinement type as a Rocq/Iris definition and
   proves a few lemmas about it. This is the hardest and rarest kind of
   writing, and the definitions are reusable across the codebase.

Rough expectation, based on RefinedC's published case studies: for
straight-line and array code, 1 is enough and the proof is fully automatic;
for code with loops and arithmetic, 1 plus a few lines of 2; for code with
custom data structures, all three, with 3 being a one-time cost per
structure. What the LLM must therefore be good at is: reading C and
guessing ownership and invariants, reading Lithium's stuck-goal output,
and writing short Rocq proofs about arithmetic and lists.

**Callers are checked against callee annotations, never bodies.** External
functions (libc, syscalls) are declared with annotations and no body; those
annotations are trusted and marked as such in the ledger.

## 5. The flow, per function

```
 C repository
   |
   | [A] capture build: compile_commands.json, target triple, flags
   v
 preprocessed translation units
   |
   | [B] bug hunting: test suite under Cerberus interpreter + sanitizers,
   |     CBMC bounded check. Any hit -> ledger as "bug found", report.
   v
   | [C] RefinedC front-end (Cerberus) -> Rocq term per function,
   |     plus a list of unsupported constructs -> ledger as "unsupported"
   v
 per function, in attack-surface order:
   |
   | [D] LLM proposes annotations (kind 1 above)
   |     context: the function, its callees' annotations, similar
   |     already-verified functions from this repo, RefinedC docs
   v
   | [E] run refinedc check. Three outcomes:
   |       ok            -> go to [G]
   |       Lithium stuck -> the typing step and goal are shown; usually a
   |                        wrong ownership description or missing loop
   |                        invariant -> back to [D] with the goal
   |       pure goals    -> go to [F]
   v
   | [F] LLM closes leftover Rocq goals (kind 2), with coq-lsp feedback;
   |     try hammer first, then LLM tactics, then helper lemmas.
   |     Stuck after budget -> ledger as "unresolved", keep partial work.
   v
   | [G] Qed succeeded. Check Print Assumptions against the allow-list.
   |     Check the annotations are not vacuous (unsatisfiable requires).
   |     Record proof hash, tool versions, assumptions -> ledger "verified".
```

Guardrails: generated files may not contain `Admitted` or new `Axiom`s;
the semantics library and the trusted external annotations live in
directories the agent cannot write to; every verified claim's assumption
set is computed, not declared.

## 6. Surviving change

A function's proof depends on its own body and on the annotations of the
functions it calls, not their bodies. So editing internals invalidates one
proof; changing a function's annotations invalidates it plus its direct
callers; everything else stays proven.

Cache every result keyed by a hash of (function body, callee annotations,
RefinedC and Rocq versions, target). On a new commit only cache misses
re-run, and the old annotations and proof are tried first since they
usually still work. Rocq proofs are more brittle than SMT-based ones to
small changes, so the retry-with-old-proof-as-hint step matters more here.

## 7. Ledger

Per (repo, commit, file, target, function): status in
{unsupported, not attempted, annotated, verified, bug found, unresolved},
the proof artifact hash, tool versions, cost, the set of trusted
annotations and axioms it depends on, and whether a human reviewed the
external annotations it uses. Proofs live in-tree under `verification/`
mirroring the source, pinned to commit hashes. Coverage is reported
weighted by attack-surface rank. A scheduled from-scratch rebuild guards
against cache rot.

## 8. First experiment: can an LLM drive RefinedC?

Cheap and decisive; do it before building the pipeline.

1. Install Rocq, Iris, RefinedC and Cerberus (opam). Confirm the RefinedC
   examples build.
2. Collect ~30 real C functions with loops and pointer arithmetic from
   small projects (musl string routines, a base64 codec, cJSON, zlib
   chunks). Mix clean and subtly buggy.
3. For each, give a frontier model the function, the RefinedC
   documentation and a handful of worked examples in context. Ask for
   annotations. Run `refinedc check`. Feed back the output. Allow N
   retries.
4. For functions that reach leftover Rocq goals, let the model write
   tactics with error feedback.
5. Measure: fraction fully verified; retries; how often the model narrows
   the precondition to make the proof go through (cheating), which must be
   detected; and whether buggy functions correctly fail.
6. Run the same set through VST as a control, to check whether the
   RefinedC choice costs much in LLM success rate.

Decent success with feedback confirms the plan. Near-zero means better
retrieval or fine-tuning on harness-generated data before the pipeline.

## 9. Roadmap

1. **Weeks 1–3:** the experiment in §8.
2. **Month 1–2:** stages A–C on 3–5 small projects. Output: per project,
   how much code the front-end accepts, what is unsupported, and what the
   bug finders catch with zero LLM work.
3. **Month 2–4:** stages D–G, the LLM loop, with cheating detection, a
   per-function budget, and retrieval of already-verified functions as
   examples.
4. **Month 4–5:** ledger and the change-tracking cache. Run on every
   commit of one live project for a month.
5. **Later:** concurrency (RefinedC is built on Iris, which supports it),
   custom data-structure types library, other targets, and then the
   properties beyond safety that motivated choosing a proof assistant.

## 10. Glossary

- **Undefined behaviour (UB):** an operation the C standard gives no
  meaning to. Compilers assume it never happens; anything can result.
- **Implementation-defined:** left to the platform but documented, e.g.
  the size of `int`.
- **Rocq:** the proof assistant; a language for theorems and a small kernel
  that checks proofs.
- **RefinedC:** the Rocq library that defines what C means and provides
  the types, automation and front-end used here.
- **Caesium:** RefinedC's definition of C semantics.
- **Cerberus:** the strict ISO-C front-end that parses C into Rocq terms;
  also usable as an interpreter to catch UB while running tests.
- **Lithium:** RefinedC's automation engine that turns "does this function
  type-check" into leftover pure goals.
- **Iris:** the separation-logic framework RefinedC is built on; separation
  logic is the style of reasoning that tracks who owns which memory.
- **Refinement type:** a type plus a fact, e.g. "an int that is less than
  n". RefinedC's annotations are written in these.
- **Loop invariant:** a fact true before every loop iteration; the hint
  that lets automation handle loops.
- **Tactic:** a Rocq command that makes proof progress; `lia` proves linear
  arithmetic facts automatically.
- **Hammer (CoqHammer, SMTCoq):** tactics that ask an external prover or
  SMT solver for a proof and then reconstruct it so the Rocq kernel checks
  it. Untrusted speed-up.
- **CBMC:** bounded model checker; exhaustive bug finder up to a loop
  depth, proves nothing.
- **Print Assumptions:** the Rocq command that lists every axiom a theorem
  depends on; the audit that catches smuggled assumptions.
