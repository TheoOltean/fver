# RefinedC annotation reference (for fver)

RefinedC verifies C functions against *refinement types* written as C2x
attributes `[[rc::<name>("...", ...)]]` placed immediately before a function,
a loop, a struct or a field. Every argument is a string literal. RefinedC's
automation (Lithium) type-checks the function; whatever pure facts remain are
handed to `solve_goal` (lia, set_solver, simplification) and then to any
`rc::tactics` you give. The verified property is *the function is free of
undefined behaviour and satisfies its contract*. Everything below was checked
against RefinedC as installed; where the syntax is subtle, an example that
verifies is given.

Two syntaxes coexist and must not be mixed:

- **annotation syntax** inside attribute strings: `int<i32>`, `&own<T>`,
  `array<i32, {...}>`, `x @ T`, `∃ r. T`;
- **Coq syntax** inside braces `{...}`: `int i32`, `&own ty`, `x @ int i32`,
  `l `at_type` int i32`, `replicate n (uninit (it_layout i32))`,
  `n ≤ max_int i32`. Braces are the only way to write arithmetic, list
  functions and Coq propositions. Writing annotation syntax such as
  `int<i32>` inside braces makes Rocq reject the generated spec.

## 1. Where annotations go

```c
#include <stddef.h>
#include <refinedc.h>          // always present; the harness adds it if missing

[[rc::parameters("n : nat")]]  // function attributes: contiguous block, no blank lines,
[[rc::args("n @ int<size_t>")]]//   directly above the definition (or a prototype)
[[rc::returns("void")]]
void f(size_t n) {
  [[rc::exists("i : nat")]]    // loop attributes: directly above for/while/do
  [[rc::inv_vars("i : i @ int<size_t>")]]
  [[rc::constraints("{i ≤ n}")]]
  for (size_t i = 0; i < n; i++) { }
}
```

Struct annotations go *after* the `struct` keyword, before the tag, and each
field carries `rc::field`:

```c
struct
[[rc::refined_by("xs : {list Z}")]]
buf {
  [[rc::field("&own<array<i32, {xs `at_type` int i32}>>")]]
  int *data;
  [[rc::field("{length xs} @ int<size_t>")]]
  size_t len;
};
```
A value of that struct then has type `xs @ buf`, and a pointer to it
`b @ &own<xs @ buf>`.

Attributes of the same kind may be repeated; the arguments are concatenated
in order. Attributes may sit on a prototype instead of the definition:
that is how contracts of already-verified callees and of external functions
(libc) are supplied to you, and calls are checked against them.

## 2. Types (annotation syntax)

| Type | Meaning |
|---|---|
| `int<it>` | an integer of C type `it`: `i8 i16 i32 i64 u8 u16 u32 u64 size_t ssize_t`; also `builtin_boolean` for `_Bool` |
| `v @ int<it>` | the integer whose mathematical value is `v` (a Coq `Z`, or a `nat` parameter) |
| `void` | the return type of a `void` function |
| `&own<T>` | an owned (exclusive, writable) pointer to memory of type `T` |
| `&shr<T>` | a shared read-only pointer to memory of type `T` |
| `p @ &own<T>` / `p @ &shr<T>` | the same, naming the location `p : loc` so `rc::ensures` can talk about it |
| `array<ly, {tys}>` | an array whose *elements have layout* `ly` (an int type such as `i32`, `u8`, `size_t`, or `{layout_of struct_x}`, or `{it_layout i32}`) and whose element *types* are the Coq list `tys` (one `type` per element) |
| `xs @ name` | a value of annotated struct `name` refined by `xs` (the `rc::refined_by` variables) |
| `optional<T>` | either `T` or `null` (e.g. a pointer that may be NULL); `optional<T, {null}>` explicitly |
| `null` | the null pointer |
| `uninit<ly>` (annotation) / `uninit ly` (Coq) | uninitialised memory of layout `ly` |
| `∃ r. T` | existential: some `r` such that `T`; usually written with `rc::exists` instead |
| `T & {P}` | `T` constrained by Coq proposition `P` |
| `function_ptr<{fn_type}>` | a function pointer with the given (Coq) function type |

Element lists for arrays, in Coq syntax inside braces:

- `{xs `at_type` int i32}` — the elements of `xs : list Z`, each an `int i32`. This is the normal way to type an initialised array.
- `{replicate n (uninit (it_layout i32))}` — `n` uninitialised ints.
- `{replicate i (0 @ int i32) ++ replicate (n - i) (uninit (it_layout i32))}` — the first `i` written, the rest not (a typical loop invariant). Note `0 @ int i32` in Coq syntax.
- `{take i xs ++ drop i ys}`, `{<[i := v]> xs}` (stdpp list insert) also work, but goals about them usually need a helper lemma.

Refinement variables are declared with `rc::parameters` (universally
quantified, in scope for the whole spec) and `rc::exists` (existentially
quantified: on a function, for `rc::returns`/`rc::ensures` only; on a loop,
for the whole invariant). Common Coq types: `Z` (integers), `nat` (naturals,
use for lengths and indices), `list Z`, `loc` (a memory location), `bool`,
`type`.

`n @ int<size_t>` with `n : nat` is fine; the range facts
`0 ≤ n ≤ max_int size_t` are available as hypotheses.

## 3. Function contracts

| Attribute | Content |
|---|---|
| `rc::parameters("x : T", ...)` | universally quantified Coq variables |
| `rc::args("T1", "T2", ...)` | one type per C parameter, in order; count must match |
| `rc::requires("{P}", ...)` | preconditions, Coq propositions in braces; also `own p : T` for extra ownership |
| `rc::exists("r : T", ...)` | existentials for the postcondition |
| `rc::returns("T")` | type of the return value (`void` for void) |
| `rc::ensures("own p : T", "{P}", ...)` | what holds on return: ownership handed back and facts |
| `rc::tactics("all: try (...).")` | Ltac appended to the proof for leftover side conditions; always wrap in `try` |
| `rc::lemmas("name")` | lemmas the automation may `apply:` |

Ownership discipline: every pointer argument that the code reads or writes
must be typed. Memory you receive as `&own<T>` disappears from the context
when the function returns unless you hand it back with
`rc::ensures("own p : T'")` (then `T'` describes its contents after the call,
and callers get it back). Memory received as `&shr<T>` is automatically
available again to the caller (no `ensures` needed) but cannot be written.

Integers: arithmetic on `int<it>` must stay in range or the proof fails with
a goal like `x + y ≤ max_int i32`. Add preconditions with `rc::requires`, or
name the values (`x @ int<i32>`) so the goal can be stated. Casts and
comparisons generate similar goals. `size_t` subtraction `n - i` needs
`{i ≤ n}` somewhere in scope.

Return type patterns:

```c
[[rc::exists("r : Z")]]
[[rc::returns("r @ int<i32>")]]
[[rc::ensures("{-1 ≤ r}", "{r < n}")]]
```
```c
[[rc::returns("{Z.min a b} @ int<i32>")]]       // exact value
[[rc::returns("{x + 1} @ int<i32>")]]
```

Preconditions use `≤` `<` `=` `≠` `∧` `∨` `¬` `→` and Coq functions
(`length xs`, `xs !! i = Some v`, `max_int i32`, `min_int i32`,
`int_modulus size_t`). ASCII `<=` also works; `≤` is preferred.

## 4. Loop invariants

Every loop needs:

```c
[[rc::exists("i : nat")]]                          // variables that change per iteration
[[rc::inv_vars("i : i @ int<size_t>",              // type of each C variable that changes
               "p : p @ &own<array<i32, {...}>>")]] //   (parameters keep their arg type if omitted)
[[rc::constraints("{i ≤ n}")]]                     // facts that hold at the loop head
for (...) ...
```

Rules that matter:

- `inv_vars` names *C local variables or parameters* and gives their type at
  the loop head. A parameter not listed keeps its `rc::args` type. If the
  loop writes through a pointer, that pointer's `inv_vars` type must describe
  the memory after `i` iterations.
- The loop counter's bound (`{i ≤ n}`) is almost always needed; `n - i`
  is otherwise meaningless on naturals.
- The invariant must be re-established after each iteration; the goals you
  get on failure say `Case distinction (if bool_decide (i < n)) -> true` for
  the step case and `-> false` for the exit case.
- `while` and `do` loops are annotated the same way; nested loops each get
  their own block.

## 5. Side conditions, tactics and helper lemmas

After typing, RefinedC prints unsolved goals as

```
Cannot solve side condition in function "zero" !
Location: "file.c" [ line : col - line : col ]
Case distinction (if bool_decide (i < n)) -> true
Goal:
n : nat
...
---------------------------------------
(list_subequiv [i] (replicate i x ++ ...) (replicate (i + 1) x ++ ...))
```

Options, in order of preference:

1. Strengthen the annotations so the fact follows by linear arithmetic.
2. `[[rc::tactics("all: try (...).")]]` with a small tactic: `lia`, `nia`,
   `exists i; split; [done|]; ...`, `have -> : (n - i = 0)%nat by lia`.
   Every tactic line is applied to *all* remaining goals, so wrap it in
   `try (...)`. A tactic that errors (not fails) aborts the whole proof.
3. A helper lemma in `lemmas.v`, applied from `rc::tactics`:

```coq
From refinedc.typing Require Import typing.

Section lemmas.
  Context `{!typeG Σ}.          (* needed whenever the lemma mentions `type` *)

  Lemma zero_step (x u : type) (i n : nat) :
    (i < n)%nat →
    list_subequiv [i] (replicate i x ++ replicate (n - i) u)
                      (replicate (i + 1) x ++ replicate (n - (i + 1)) u).
  Proof. ... Qed.
End lemmas.
```
   then `[[rc::tactics("all: try (apply zero_step; lia).")]]`. Pure integer
   lemmas need no Section. `max_int i32` does not reduce for `lia`; first
   `have -> : max_int i32 = 2147483647 by vm_compute.` Do not write
   `//@rc::import`: the harness imports `lemmas.v` for you.

Facts about the goal language: `list_subequiv is l1 l2` means `length l1 =
length l2 ∧ ∀ j, j ∉ is → l1 !! j = l2 !! j`. Hypotheses about `nat`
parameters appear as `Z`-level facts (`i ≤ n`); `lia` handles `Z.of_nat`.
`(0 @ int<i32>)%I` in a printed goal is the Coq term `0 @ int i32`.

## 6. Ghost statements (from refinedc.h)

No-ops at runtime, allowed in function bodies: `rc_unfold_int(i);` (make the
range of an integer parameter available before its first use),
`rc_unfold(e);`, `rc_unlock(e);`, `rc_to_uninit(e);`, `rc_share(e);`,
`rc_learn(e);`, `rc_stop(e);`. Do not remove or reorder real code around
them.

## 7. How failures read

| Message | Meaning | What to change |
|---|---|---|
| `Type system got stuck in function "f" in block "#k" !` with a goal containing `typed_write_end ... Shr`, `typed_read_end ... uninit`, `Goto`, `typed_if` | the ownership/typing annotations do not describe the program at that point: writing through `&shr`, reading memory typed `uninit`, a loop without an invariant | annotations, not tactics |
| `Cannot solve side condition in function "f" !` | the program type-checks; a pure fact is left | constraints / requires, then tactics, then a lemma |
| `[file:line:col] Frontend error.` / `Not implemented: ...` / `feature not yet supported` | the C front-end rejected the file (real source line numbers): unsupported construct or malformed attribute | fix the attribute; if the construct is unsupported, say `UNSUPPORTED:` in the Note |
| `refinedc: internal error, uncaught exception` | a malformed attribute string crashed the parser | fix the attribute syntax |
| `File ".../generated_spec.v", ... Error: Syntax error` | annotation syntax used inside `{...}` | write Coq inside braces |
| `File ".../lemmas.v", ... Error:` | your lemma does not compile | fix the lemma |

Coq-level locations (`Location: "f.c" [ 134 : 21 - 134 : 26 ]`) are in
preprocessed coordinates; the harness translates them to source lines and
quotes the line when it can. Columns are exact.

Unsupported by the front-end, regardless of annotations: floating-point
arithmetic, comparison and conversion (see below), variadic functions
(`va_arg`/`va_end`), casts inside integer constant expressions
(`char b[(int)(8 * sizeof(void *))]`), inline assembly, `_Atomic`, copying a
whole union value. Supported: `goto`, `switch`, function pointers
(`function_ptr<...>`), bitwise operations, `static` locals as globals,
`<setjmp.h>` (as an include; a function that calls `setjmp`/`longjmp` cannot
be verified).

### Floating point is opaque

The checker sees a copy of the code in which `float`, `double` and `long
double` are replaced by same-size structs with no arithmetic: `struct
fver_f32`, `struct fver_f64`, `struct fver_fld`. Consequences:

- A function that only stores, copies, passes or returns float values can be
  verified; one that computes with them is reported unsupported. Do not try
  to annotate around this.
- In annotations a float is a struct: the layout of a `double` field or
  variable is `struct_fver_f64` (`struct_fver_f32`, `struct_fver_fld`). Type a
  float you never read as `uninit<struct_fver_f64>`; `lua_Number`-style
  typedefs resolve to the same layouts.
- Structs you cannot annotate (they live in headers) are typed field by
  field with `struct<struct_Tag, ty1, ty2, ...>`; a union member you do not
  touch is `uninit<union_Tag>`. Example, a tagged value whose union holds a
  double: `&own<struct<struct_TValue, uninit<union_Value>, t @ int<i32>>>`
  lets the function read and compare the tag `t`. Reading or writing a union
  *member* needs the union annotated with `rc::union_tag` at its definition,
  which is not available from a function-only submission; say
  `UNSUPPORTED:` in the Note if the function needs it.

## 8. Strict rules

- Only add: attributes, ghost statements, comments. The code must otherwise
  stay byte-for-byte the same; the harness rejects any change before the
  checker runs.
- Never use `rc::trust_me`, `rc::skip`, `rc::manual_proof`,
  `rc::annot_args`, or the comment directives `//@rc::import`,
  `//@rc::require`, `//@rc::inlined*`, `//@rc::context`, `//@rc::typedef`.
- Never write `Admitted`, `admit`, `Axiom`, `Parameter`, `Hypothesis`,
  `Conjecture`, `Unset ... Checking` in lemmas.v.
- Preconditions must be ones real callers can satisfy; they become the
  callers' obligations. An unsatisfiable `rc::requires` is rejected.
- If the code really can exhibit undefined behaviour for inputs the callers
  can produce, say so: a line starting with `BUG:` and the triggering input,
  instead of forcing a proof with an unrealistic precondition.
