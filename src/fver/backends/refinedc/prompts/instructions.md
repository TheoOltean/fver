# Task

You are given one C function, the file it lives in for context, the
contracts of the functions it calls (already verified or trusted), and the
target platform. Produce RefinedC annotations that make the checker accept
the function as free of undefined behaviour.

Output format, exactly:

1. One fenced block for the annotated function, opened as
   ` ```c file=function.c ` and containing the *complete* function
   definition (attributes + signature + body). No other code, no prose.
2. Optionally, one fenced block ` ```coq file=lemmas.v ` with helper lemmas,
   only when a pure side condition genuinely needs one.
3. Optionally, a single line starting with `Note:` explaining a design
   choice or a reason the function cannot be verified as written.

Rules:

- The code inside function.c must be byte-for-byte the original apart from
  added `[[rc::...]]` attributes, ghost statements from refinedc.h, and
  comments. Do not reformat, rename, reorder or "fix" anything. If the code
  has a real bug (an out-of-bounds read that no precondition can rule out
  without making the function uncallable), say so in the Note and give the
  strongest honest precondition.
- Never use `rc::trust_me`, `rc::skip`, `rc::manual_proof`, or `rc::import`.
- Never write `Admitted`, `admit`, `Axiom`, `Parameter`, `Hypothesis` in
  lemmas.v.
- Preconditions must be satisfiable by real callers. They become proof
  obligations on those callers.
- When the checker's feedback from a previous attempt is included, address
  that feedback specifically. If the automation was stuck on a typing goal,
  change the ownership annotations, not the tactics.
- Prefer the weakest precondition that still lets the proof go through, and
  the simplest invariant. Do not specify functional behaviour that no caller
  needs.
