# Task

You are given one C function, the file it lives in for context, the
contracts of the functions it calls (already verified, as prototypes with
ACSL blocks), and the target platform. Produce ACSL annotations that make
Frama-C/WP accept the function as free of undefined behaviour.

Output format, exactly:

1. One fenced block for the annotated function, opened as
   ` ```c file=function.c ` and containing the *complete* function
   definition: the `/*@ ... */` contract, the signature, the body with its
   loop annotations. No other code, no prose inside the block.
2. Optionally, a single line starting with `Note:` explaining a design
   choice, or `UNSUPPORTED:` if Frama-C cannot handle a construct the
   function needs, or `BUG:` with a concrete input if the code really has
   undefined behaviour that no honest precondition rules out.

Rules:

- The code inside function.c must be the original apart from added ACSL
  comments (`/*@ ... */` and `//@ ...`). Do not reformat, rename, reorder or
  "fix" anything.
- Every function needs `assigns`; every loop needs `loop invariant` and
  `loop assigns` (`loop variant` is optional; termination is not required).
- Never use `admit`, `axiom`, `axiomatic`, `assumes` or ghost code.
- Preconditions must be satisfiable by real callers; they become proof
  obligations on those callers. Prefer the weakest precondition that lets
  the proof go through and the simplest invariant. Do not specify
  functional behaviour that no caller needs.
- When checker feedback from a previous attempt is included, address it
  specifically, goal by goal. `GOALS_REMAIN` lists each unproved goal with
  its meaning and the printed goal. `FRONTEND_ERROR` means fix the ACSL
  syntax or report `UNSUPPORTED:`.
- Calls to C library functions (memcpy, strlen, malloc, ...) use Frama-C's
  own ACSL specifications; establish their preconditions (valid ranges,
  `\valid_read_string`) in yours.
- A callee from this project without a contract cannot be reasoned about;
  if the task lists such a callee, say so in a `Note:` and still submit the
  best contract for the function itself.
