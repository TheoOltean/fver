# ACSL for Frama-C/WP (for fver)

The checker is Frama-C with the WP plugin. The RTE plugin turns every
potential undefined behaviour in the function into an assertion (memory
access, signed overflow, division by zero, shifts, invalid pointer
arithmetic, array bounds), and WP proves those assertions, your contract
and your loop annotations with SMT solvers. Your job is to write the
contract and loop annotations that make every goal provable.

## 1. Where annotations go

A function contract is a `/*@ ... */` comment immediately above the
definition. Loop annotations are a `/*@ ... */` comment immediately above
the loop. Assertions are `//@ assert P;` statements between C statements.
Nothing else changes: same code, same names, same order.

```c
/*@ requires \valid_read(a + (0 .. n-1));
    requires 0 <= n;
    assigns \nothing;
    ensures \result >= 0;
*/
int count_zero(const int *a, int n) {
  int c = 0;
  /*@ loop invariant 0 <= i <= n;
      loop invariant 0 <= c <= i;
      loop assigns i, c;
      loop variant n - i;
  */
  for (int i = 0; i < n; i++)
    if (a[i] == 0) c++;
  return c;
}
```

## 2. Contract clauses

| Clause | Meaning |
|---|---|
| `requires P;` | precondition; callers must establish it. Becomes a proof obligation on every caller. |
| `assigns L;` | every memory location the function may write, as a comma-separated list, or `\nothing`. **Mandatory**: without it WP assumes the function writes everywhere and every caller's proof dies. Local variables are not listed; `*p`, `p[0 .. n-1]`, `s->field`, `\result` are. |
| `ensures P;` | postcondition. `\result` is the return value; `\old(e)` the value of `e` at entry. Only what callers need. |
| `terminates \true;` | not needed. |

Memory predicates:

| Predicate | Meaning |
|---|---|
| `\valid(p)` | `*p` may be read and written |
| `\valid_read(p)` | `*p` may be read |
| `\valid(p + (0 .. n-1))` | `n` writable elements starting at `p`; `n` may be 0 (empty range is fine) |
| `valid_read_string(s)` / `valid_string(s)` | (no backslash) `s` points to a NUL-terminated string that may be read / written; `strlen(s)` is then a logic function. Both come from Frama-C's `<string.h>`, which the file must include (it does if the code calls string functions) |
| `\separated(p + (0..n-1), q + (0..m-1))` | the two ranges do not overlap (needed when one is written and the other read) |
| `\null`, `\true`, `\false` | as expected |
| `\offset(p)`, `\block_length(p)` | position inside and size of the allocated block; rarely needed |

Integer logic is mathematical (unbounded `integer`), so `x + 1` in ACSL
never overflows; the RTE assertion is about the C expression. Use
`INT_MAX`, `INT_MIN`, `UINT_MAX` etc. from `<limits.h>` in contracts.

Common precondition patterns:

- Pointer parameter read only: `requires \valid_read(p);`
- Pointer parameter written: `requires \valid(p);` and `assigns *p;`
- Buffer of `n` bytes: `requires \valid(buf + (0 .. n-1));` plus `requires 0 <= n;` for signed `n`.
- C string parameter: `requires valid_read_string(s);` and use `strlen(s)` in invariants.
- Writing through a pointer stored in a struct (`s->buf[i] = ...`): add
  `requires \separated(s, s->buf + (0 .. n-1));`, otherwise WP must assume the
  write may have changed `s->n` and the loop invariant cannot be preserved.
- Struct pointer: `requires \valid(it);` covers every field; add `requires \valid(it->child)` for pointers inside.
- Nullable pointer: `requires p == \null || \valid(p);`
- Arithmetic that could overflow: bound the inputs, e.g. `requires 0 <= n <= INT_MAX - 1;`
- Callee preconditions: they become your obligations at the call; make sure your own `requires` implies them.

## 3. Loop annotations

Every loop needs all of:

- `loop invariant P;` for each fact that holds before every iteration test. Always include the counter's range (`0 <= i <= n`, note `<=` on the upper bound, since the invariant holds when the loop exits). If the body reads or writes through a pointer that moves, say where it is (`p == \at(p, Pre) + i`).
- `loop assigns x, y, a[0 .. n-1];` every variable and location the body writes, including the counter. Missing an item gives an unprovable `loop_assigns` goal; listing a location the invariant says is unchanged breaks nothing.
- `loop variant e;` is optional: termination is not undefined behaviour and the harness does not ask for it.

An invariant must be strong enough to prove the RTE assertions inside the
body **and** to re-establish itself. When a goal `*_loop_invariant_preserved`
fails, the invariant is missing a fact that the next iteration needs. When
`*_established` fails, it is false on entry (check the empty-loop case).

For `while (*p)` string loops: `loop invariant 0 <= i <= strlen(s);` and
`loop invariant \valid_read(s + (0 .. strlen(s)));` (from the precondition,
restated so WP keeps it).

## 3b. Calls through function pointers

WP cannot reason about `p->fn(x)` or `table.fn(x)` unless told which
functions the pointer may hold. Put a `calls` statement annotation on the
line before the statement that makes the call, and a precondition that
pins the pointer:

```c
/*@ requires \valid_read(hooks);
    requires hooks->allocate == malloc;
    assigns \nothing;
    ensures \result == \null || \valid(\result);
*/
static void *alloc_one(const struct hooks *hooks) {
  //@ calls malloc;
  void *p = hooks->allocate(16);
  return p;
}
```

If the pointer may hold one of several functions, list them all:
`//@ calls malloc, my_alloc;` with `requires hooks->allocate == malloc ||
hooks->allocate == my_alloc;`. A global table (`global_hooks.deallocate`) is
handled the same way, with the precondition on the global. The harness
accepts `calls` before a declaration with an initialiser and rewrites it
for Frama-C.

## 4. Quantifiers and logic

`\forall integer k; 0 <= k < n ==> a[k] == 0;`, `\exists integer k; ...`,
`==>`, `<==>`, `&&`, `||`, `!`. Operators on `integer` are exact. Casts:
`(integer) x`. Sizes: `sizeof(T)`. Struct fields `s->f`, `s.f`; array
elements `a[i]`; pointer arithmetic `p + i`.

## 5. How failures read

`WP proved 7 of 9 goals` then a list of goal names with a one-line
meaning. Goal names encode the property:

- `..._assert_rte_mem_access`: a read or write at the named line is not
  covered by any `\valid`/`\valid_read` fact WP can see there. Add or widen a
  precondition, or the loop invariant that carries it into the loop.
- `..._assert_rte_signed_overflow`: bound the operands with a precondition or
  invariant.
- `..._loop_invariant_established` / `_preserved`, `_loop_assigns`: see
  section 3.
- `..._call_<callee>_requires`: your facts at the call do not imply the
  callee's precondition.
- `..._ensures` / `..._assigns`: your own post-state claims. Weaken them or
  add what proves them.

The printed goal shows hypotheses above the line and the property below;
the property is what WP could not prove from the hypotheses.

## 6. Strict rules

- No `admit`, `axiom`, `axiomatic`, `assumes`, `ghost`, no `#include` or
  `#define`. The harness rejects them before checking.
- No `requires \false` or other unsatisfiable preconditions.
- Preconditions must be what a real caller can promise. Everything in
  `requires` is a proof obligation on the callers; the weakest precondition
  that makes the proof go through is the right one.
- Postconditions only as far as callers need them for their own
  undefined-behaviour proofs: for a function returning a length, a range;
  for one returning a pointer, validity or NULL; nothing about values that
  no caller relies on.
