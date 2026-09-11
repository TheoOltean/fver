# RefinedC annotation reference (for fver)

RefinedC verifies C functions inside Rocq (formerly Coq) using separation
logic. You describe *what memory each argument owns* and *which pure facts
hold*; the automation (Lithium) then proves the function is free of undefined
behaviour: no out-of-bounds access, no use of freed or uninitialised memory,
no null dereference, no signed overflow, no invalid pointer arithmetic.

The goal in fver is only UB-freedom. Do not try to specify what the
function computes unless a caller needs that fact for its own safety; the
minimum honest ownership description that makes the proof go through is
the right answer.

> This reference was written without access to a RefinedC install. Where it
> disagrees with the `examples/` directory of the RefinedC repository, the
> repository wins. The checker's error output is authoritative.

## 1. Where annotations go

Annotations are C2x attributes `[[rc::NAME("arg", "arg", ...)]]`. Every
argument is a string literal. Attributes are placed:

- immediately before a function definition or prototype (contract);
- immediately before a `for`, `while` or `do` loop (loop invariant);
- immediately before a `struct` definition or field (data-structure types);
- immediately before a global variable.

The file must `#include <refinedc.h>`; fver adds the include for you.

## 2. Types

| Syntax | Meaning |
|---|---|
| `int<i32>`, `int<u8>`, `int<i64>`, `int<u64>`, `int<size_t>`, `int<uintptr_t>`, `int<char_it>` | an integer of that C type, any value in range |
| `n @ int<i32>` | an integer whose mathematical value (a Coq `Z`) is `n` |
| `boolean<bool_it>`, `b @ boolean<bool_it>` | a boolean |
| `void` | the unit result |
| `&own<T>` | an owned (exclusive, writable) pointer to memory of type `T` |
| `&shr<T>` | a shared (read-only) pointer to `T` |
| `p @ &own<T>` | the owned pointer whose location is the Coq variable `p : loc` |
| `null` | the null pointer |
| `optional<T, null>` | either `T` or null (use `optional<&own<T>, null>` for nullable pointers) |
| `array<int<i32>, {tys}>` | a contiguous array whose element *types* are the Coq list `tys`; each element has the layout of `int<i32>` |
| `array<int<i32>, {replicate n (uninit (it_layout i32))}>` | `n` uninitialised ints |
| `array<int<i32>, {xs `at_type` int<i32>}>` | ints whose values are the list `xs : list Z` |
| `uninit<{it_layout i32}>` | uninitialised memory with that layout |
| `any<{ly}>` | some value of layout `ly`, contents unknown |
| `struct<struct_name, T1, T2, ...>` | a struct with those field types, in declaration order |
| `∃ x. T` | a type with an existentially quantified Coq variable (e.g. `∃ n. n @ int<i32>`) |
| `T & {P}` | `T` together with a pure Coq proposition `P` about its refinements |
| `place<p>` / `value<v, T>` | rarely needed; a location / a specific value |

Inside `{ ... }` you write ordinary Coq/stdpp terms over the variables from
`rc::parameters` and `rc::exists`. Integers are `Z`; list functions are
`length`, `replicate`, `take`, `drop`, `<[i := x]>` (list insert), `!!`
(lookup). Bounds on C types are `max_int i32`, `min_int i32`, `max_int u64`,
etc. Use `%nat` when a literal must be a `nat`, and `Z.of_nat` / `Z.to_nat`
to convert.

**Unicode.** RefinedC expects `≤`, `≥`, `≠`, `∃`, `∀`, `→`, `∧`, `∨`, `¬`,
`∈`. ASCII alternatives are accepted inside Coq braces for most of them
(`<=`, `>=`, `<>`, `->`, `/\`, `\/`, `~`), but `∃` in *types* must be the
unicode symbol. Prefer unicode consistently.

## 3. Function contracts

```c
[[rc::parameters("p : loc", "n : nat", "xs : {list Z}")]]
[[rc::args("p @ &own<array<int<i32>, {xs `at_type` int<i32>}>>", "n @ int<size_t>")]]
[[rc::requires("{n = length xs}")]]
[[rc::returns("void")]]
[[rc::ensures("own p : array<int<i32>, {replicate n (0 @ int<i32>)}>")]]
```

| Attribute | Purpose |
|---|---|
| `rc::parameters("x : T", ...)` | universally quantified Coq variables the caller chooses (`nat`, `Z`, `loc`, `{list Z}`, `bool`). Every name used elsewhere must be declared here or in `rc::exists`. |
| `rc::args("T1", "T2", ...)` | one type per C parameter, in order. Pointer parameters get `&own<...>`/`&shr<...>`; scalar parameters usually get `x @ int<...>`. |
| `rc::requires("C", ...)` | preconditions. `{P}` for pure facts; `own l : T` / `shr l : T` for extra ownership not attached to an argument. |
| `rc::returns("T")` | the return type (`void`, `int<i32>`, `n @ int<i32>`, `optional<&own<T>, null>`, `∃ r. r @ int<i32>`). |
| `rc::exists("y : T", ...)` | existentially quantified variables for the postcondition (values the function chooses). |
| `rc::ensures("C", ...)` | postconditions: what ownership is handed back and which pure facts hold. Every `&own` argument's memory must be returned here with its final type, otherwise the caller loses it. |
| `rc::tactics("all: try lia.")` | Rocq tactic text run on remaining pure side conditions. Useful values: `all: try lia.`, `all: try nia.`, `all: try set_solver.`, `all: try by rewrite length_replicate.` |
| `rc::lemmas("name", ...)` | lemmas (from lemmas.v or the standard library) the solver may use. |

Rules of thumb:

- Ownership is linear. What comes in through `&own` must go out through
  `rc::ensures` (`own p : ...`). Forgetting this makes the proof fail at the
  return, or makes callers unable to use the memory afterwards.
- `&shr` needs no `ensures`; it is duplicable.
- A pointer that the function may read `n` elements through must have a type
  covering `n` elements: `&own<array<int<i32>, {replicate n (...)}>>` or the
  `at_type` form. A bare `&own<int<i32>>` covers exactly one element.
- Signed arithmetic must be shown in range. If the code computes `a + b`
  on `int`, you need `{a + b ≤ max_int i32}` (and the lower bound) to be
  provable from `rc::requires`, `rc::constraints`, or the refinement types.
- Length arguments: `n @ int<size_t>` with `n : nat` and `{n = length xs}`.

## 4. Loop invariants

Place before the loop keyword:

```c
[[rc::exists("i : nat")]]
[[rc::inv_vars("i : i @ int<size_t>",
               "p : p @ &own<array<int<i32>, {replicate i (0 @ int<i32>) ++ replicate (n - i) (uninit (it_layout i32))}>>")]]
[[rc::constraints("{i ≤ n}")]]
for (size_t i = 0; i < n; i++) p[i] = 0;
```

| Attribute | Purpose |
|---|---|
| `rc::exists("i : nat")` | variables the invariant quantifies over (typically the loop counter's mathematical value). |
| `rc::inv_vars("cvar : T", ...)` | the type of each C variable *that changes in the loop or whose ownership is needed*, at the loop head. Variables not listed keep their type from before the loop. |
| `rc::constraints("{P}", ...)` | pure facts that hold at the loop head. |

The invariant must hold on entry (with `i = 0`), be preserved by one
iteration, and, together with the negated loop condition, imply what the
code after the loop needs. Most failures are one of: a missing upper bound
(`{i ≤ n}`), an array type that does not split into the written part and
the unwritten part, or a counter typed `int<i32>` when it should be
`int<size_t>`.

## 5. Structs and globals

```c
[[rc::refined_by("n : nat", "cap : nat")]]
[[rc::exists("xs : {list Z}")]]
[[rc::constraints("{n ≤ cap}", "{length xs = cap}")]]
struct [[rc::ptr_type("vec : ...")]] vec {
  [[rc::field("n @ int<size_t>")]] size_t len;
  [[rc::field("cap @ int<size_t>")]] size_t cap;
  [[rc::field("&own<array<int<i32>, {xs `at_type` int<i32>}>>")]] int *data;
};
```

`rc::refined_by` names the mathematical refinement of the struct;
`rc::field` gives each field's type; `rc::constraints` at struct level are
invariants. A function taking such a struct writes
`p @ &own<struct<struct_vec, ...>>` or, once the struct has a `rc::typedef`,
the named type. Globals take `[[rc::global("T")]]`.

## 6. Ghost statements (from refinedc.h)

Statements the annotator may insert; they have no runtime effect:

- `rc_unfold(x);` / `rc_unfold_int(x);` — ask the automation to unfold a refinement of variable `x`.
- `rc_annot(x, "T");` — assert/convert the type of `x` to `T` at this point.
- `rc_unlock(x);` — release a shared-to-own conversion.

Use them sparingly; they are rarely needed for UB-freedom.

## 7. How checking works, and how to read failures

`refinedc check` translates the file, generates one Rocq proof per
annotated function, and runs the automation. Three kinds of failure:

1. **Front-end error** (`parse error`, `unknown attribute`, `unsupported`):
   the attribute text is malformed, a name is undeclared, or the C construct
   cannot be handled. Fix syntax; if the construct is unsupported, say so in
   the note.
2. **Automation stuck** (the printed goal contains a typing judgement such as
   `◁`, `typed_...`, `find_in_context`, `Cannot find ...`): the annotations do
   not describe the memory the code touches at that point. Repair the
   ownership or loop types. Adding tactics does not help here.
3. **Pure side condition remains** (the goal is arithmetic or list algebra
   without typing judgements): add a `rc::constraints`/`rc::requires` fact
   that makes it obvious, or add `rc::tactics("all: try lia.")`, or prove a
   helper lemma in lemmas.v and cite it with `rc::lemmas`.

A Rocq goal looks like hypotheses, a line of `====`, then the conclusion.
Read the conclusion first.

## 8. Strict rules

- Do **not** change the code. Only attributes, ghost statements and comments
  may be added. Renaming a variable, changing `<` to `<=`, adding a check,
  reordering statements: all rejected before the checker even runs.
- Do **not** use `rc::trust_me`, `rc::skip`, `rc::manual_proof`, or
  `rc::import`. fver wires lemmas.v in itself.
- lemmas.v may not contain `Admitted`, `admit`, `Axiom`, `Parameter`,
  `Hypothesis`, `Conjecture` or any `Unset ... Checking`.
- Do not weaken the precondition to make the function uncallable
  (`{False}`, `{0 = 1}`, typing every pointer as `null`). Preconditions
  must be ones a real caller can satisfy; they become obligations on
  callers, which fver verifies too.
- Do not add `#include` lines.
