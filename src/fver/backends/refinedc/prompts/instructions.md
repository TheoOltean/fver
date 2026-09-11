# Task

You are given one C function, the file it lives in for context, the
contracts of the functions it calls (already verified or trusted, as
attributed prototypes), and the target platform. Produce RefinedC
annotations that make the checker accept the function as free of undefined
behaviour.

Output format, exactly:

1. One fenced block for the annotated function, opened as
   ` ```c file=function.c ` and containing the *complete* function
   definition (attributes + signature + body). If the function uses a struct
   that is not annotated yet, put the annotated struct definition in the same
   block, above the function. No other code, no prose inside the block.
2. Optionally, one fenced block ` ```coq file=lemmas.v ` with helper lemmas,
   only when a pure side condition genuinely needs one. Start it with
   `From refinedc.typing Require Import typing.`; lemmas that mention `type`
   go in `Section s. Context `{!typeG Σ}. ... End s.`. Apply them from
   `rc::tactics`; the harness imports the file for you.
3. Optionally, a single line starting with `Note:` explaining a design
   choice, or `UNSUPPORTED:` if the front-end cannot handle a construct the
   function needs, or `BUG:` with a concrete input if the code really has
   undefined behaviour that no honest precondition rules out.

Rules:

- The code inside function.c must be the original apart from added
  `[[rc::...]]` attributes, ghost statements from refinedc.h, and comments.
  Do not reformat, rename, reorder or "fix" anything.
- Inside attribute strings use annotation syntax (`int<i32>`, `&own<T>`,
  `array<i32, {...}>`); inside `{...}` use Coq syntax (`int i32`,
  `x @ int i32`, `xs `at_type` int i32`, `uninit (it_layout i32)`).
- Never use `rc::trust_me`, `rc::skip`, `rc::manual_proof`, `rc::annot_args`,
  or any `//@rc::` comment directive.
- Never write `Admitted`, `admit`, `Axiom`, `Parameter`, `Hypothesis` in
  lemmas.v.
- Preconditions must be satisfiable by real callers. They become proof
  obligations on those callers. Prefer the weakest precondition that lets
  the proof go through, and the simplest invariant. Do not specify
  functional behaviour that no caller needs; exact return values are welcome
  only when they come for free.
- When checker feedback from a previous attempt is included, address it
  specifically. `AUTOMATION_STUCK` means change the ownership / loop
  annotations. `GOALS_REMAIN` means add constraints, then tactics, then a
  lemma. `FRONTEND_ERROR` means fix the syntax or report `UNSUPPORTED:`.
