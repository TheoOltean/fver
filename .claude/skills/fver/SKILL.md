---
name: fver
description: Prove C functions free of undefined behaviour with fver, acting as the prover yourself. Use when asked to verify, prove, or check the safety of C code in a repository that has a .fver/ directory, or after editing C code in such a repository.
---
# Proving C functions with fver

You are the prover. fver supplies the task, the rules, the proof checker,
the audit and the bookkeeping; you supply the annotations and proofs. The
checker is the only judge: nothing is verified until `fver agent check` says so.

## Goal

Get functions to `verified` status, highest attack surface first, without
changing any C code and without escape hatches.

## How it fits together

- `fver agent next` lists what to prove, callees before callers, so contracts
  exist before you need them.
- `fver agent task <fn>` prints the packet for one function: the annotation
  language reference (read it once per session, then use `--no-reference`),
  the function, its file context, the contracts of verified callees, trusted
  external specs, and the previous accepted submission if the code changed.
- Write the annotated function (the full definition, code unchanged) to a
  scratch file under `.fver/` such as `.fver/scratch/<fn>.c`. Never edit the
  user's source files. Helper lemmas, if needed, go in a second file.
- `fver agent check <fn> --submission .fver/scratch/<fn>.c [--lemmas ...]` runs
  the guardrails, the checker and the audit, records the result, and quotes
  the goal the checker could not close. Exit 0 means verified.
- Iterate on the feedback. Stop when verified, when the budget you were
  given is spent, or when the checker reports a real bug.
- If the code has a genuine defect that no honest precondition rules out,
  do not force a proof: submit a reply that starts with `BUG:` and explains
  the triggering input. fver records it as a suspected bug for a human.
- After you edit any C code, run `fver agent changed` to see which proofs went
  stale and re-prove them; a stale proof is a proof of old code.

## Rules the checker enforces

- Preconditions must be the weakest that make the function safe. A proof
  that demands `false` of the caller is rejected.
- No admitted goals, trusted functions, skipped functions or new axioms.
- The submission is the whole function every time.

## Reporting

Tell the user what is verified, what is stale, and any `BUG:` reports, in
plain words. `fver status` (and `fver status <fn>`) has the details.
