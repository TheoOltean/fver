# Worked examples

Every submission below was accepted by RefinedC exactly as written. The
pattern: declare the mathematical variables, give every pointer a type that
covers exactly the memory the code touches, hand ownership back in
`rc::ensures`, and give every loop an invariant with the counter bound.

## Example 1: zero an array (loop over owned memory, helper lemma)

Original:

```c
void zero(int *p, size_t n) {
  for (size_t i = 0; i < n; i++) p[i] = 0;
}
```

Submission:

```c file=function.c
[[rc::parameters("p : loc", "n : nat")]]
[[rc::args("p @ &own<array<i32, {replicate n (uninit (it_layout i32))}>>", "n @ int<size_t>")]]
[[rc::returns("void")]]
[[rc::ensures("own p : array<i32, {replicate n (0 @ int i32)}>")]]
[[rc::tactics("all: try (apply zero_step; lia).")]]
[[rc::tactics("all: try (exists i; split; [done|]; split; [by have -> : (n - i = 0)%nat by lia | lia]).")]]
void zero(int *p, size_t n) {
  [[rc::exists("i : nat")]]
  [[rc::inv_vars("i : i @ int<size_t>",
                 "p : p @ &own<array<i32, {replicate i (0 @ int i32) ++ replicate (n - i) (uninit (it_layout i32))}>>")]]
  [[rc::constraints("{i ≤ n}")]]
  for (size_t i = 0; i < n; i++) p[i] = 0;
}
```

```coq file=lemmas.v
From refinedc.typing Require Import typing.

Section lemmas.
  Context `{!typeG Σ}.

  Lemma zero_step (x u : type) (i n : nat) :
    (i < n)%nat →
    list_subequiv [i] (replicate i x ++ replicate (n - i) u)
                      (replicate (i + 1) x ++ replicate (n - (i + 1)) u).
  Proof.
    move => Hlt j. split.
    { rewrite !length_app !length_replicate. lia. }
    move => Hj. have Hne : j ≠ i by set_solver.
    rewrite !lookup_app.
    destruct (decide (j < i)%nat) as [Hji|Hji].
    - rewrite !lookup_replicate_2 //; lia.
    - rewrite (lookup_ge_None_2 (replicate i x)) ?length_replicate; [|lia].
      rewrite (lookup_ge_None_2 (replicate (i + 1) x)) ?length_replicate; [|lia].
      destruct (decide (j < n)%nat).
      + rewrite !lookup_replicate_2 //; lia.
      + rewrite !lookup_ge_None_2 ?length_replicate //; lia.
  Qed.
End lemmas.
```
Note: the array's element list at the loop head is split into the part
already written and the part still uninitialised. The step case leaves a
`list_subequiv` goal (proved by the lemma); the exit case needs the witness
`i` and `n - i = 0`. `array<i32, ...>`: the first argument is the element
layout, the second the Coq list of element types.

## Example 2: bounded search over shared memory (fully automatic)

Original:

```c
int find(const int *xs, size_t n, int key) {
  for (size_t i = 0; i < n; i++) {
    if (xs[i] == key) return (int)i;
  }
  return -1;
}
```

Submission:

```c file=function.c
[[rc::parameters("p : loc", "n : nat", "xs : {list Z}", "key : Z")]]
[[rc::args("p @ &shr<array<i32, {xs `at_type` int i32}>>", "n @ int<size_t>", "key @ int<i32>")]]
[[rc::requires("{n = length xs}", "{n ≤ max_int i32}")]]
[[rc::exists("r : Z")]]
[[rc::returns("r @ int<i32>")]]
[[rc::ensures("{-1 ≤ r}", "{r < n}")]]
int find(const int *xs, size_t n, int key) {
  [[rc::exists("i : nat")]]
  [[rc::inv_vars("i : i @ int<size_t>")]]
  [[rc::constraints("{i ≤ n}")]]
  for (size_t i = 0; i < n; i++) {
    if (xs[i] == key) return (int)i;
  }
  return -1;
}
```
Note: `&shr` because the array is only read, so no `ensures` is needed for
it and the caller keeps it. `{n ≤ max_int i32}` is what makes `(int)i`
safe. `xs `at_type` int i32` types an initialised array by its values.

## Example 3: two owned pointers, exact results (fully automatic)

Original:

```c
void swap(int *x, int *y) {
  int t = *x;
  *x = *y;
  *y = t;
}

int min2(int a, int b) {
  return a < b ? a : b;
}
```

Submission (one function per submission in practice; both shown for the
patterns):

```c file=function.c
[[rc::parameters("pa : loc", "pb : loc", "a : Z", "b : Z")]]
[[rc::args("pa @ &own<a @ int<i32>>", "pb @ &own<b @ int<i32>>")]]
[[rc::returns("void")]]
[[rc::ensures("own pa : b @ int<i32>", "own pb : a @ int<i32>")]]
void swap(int *x, int *y) {
  int t = *x;
  *x = *y;
  *y = t;
}
```

```c file=function.c
[[rc::parameters("a : Z", "b : Z")]]
[[rc::args("a @ int<i32>", "b @ int<i32>")]]
[[rc::returns("{Z.min a b} @ int<i32>")]]
int min2(int a, int b) {
  return a < b ? a : b;
}
```
Note: ownership of each pointer is returned with its new contents. For
UB-freedom alone, `[[rc::returns("int<i32>")]]` would also do for `min2`;
the exact value is free here and helps callers.

## Example 4: a struct with a length field (fully automatic)

Original:

```c
struct buf { int *data; size_t len; };

int buf_last(struct buf *b) {
  if (b->len == 0) return 0;
  return b->data[b->len - 1];
}
```

Submission (the struct annotation is included because the struct was not
annotated yet; write it exactly once):

```c file=function.c
struct
[[rc::refined_by("xs : {list Z}")]]
buf {
  [[rc::field("&own<array<i32, {xs `at_type` int i32}>>")]]
  int *data;
  [[rc::field("{length xs} @ int<size_t>")]]
  size_t len;
};

[[rc::parameters("b : loc", "xs : {list Z}")]]
[[rc::args("b @ &own<xs @ buf>")]]
[[rc::exists("r : Z")]]
[[rc::returns("r @ int<i32>")]]
[[rc::ensures("own b : xs @ buf")]]
int buf_last(struct buf *b) {
  if (b->len == 0) return 0;
  return b->data[b->len - 1];
}
```
Note: tying `len` to `length xs` in the struct type is what proves the
index `b->len - 1` in bounds; the `len == 0` guard makes the subtraction
safe on `size_t`.

## Example 5: signed arithmetic needing a lemma

Original:

```c
int sum_to(int n) {
  int s = 0;
  for (int i = 0; i < n; i++) s += i;
  return s;
}
```

Submission:

```c file=function.c
[[rc::parameters("n : Z")]]
[[rc::args("n @ int<i32>")]]
[[rc::requires("{0 ≤ n}", "{n ≤ 46340}")]]
[[rc::exists("r : Z")]]
[[rc::returns("r @ int<i32>")]]
[[rc::tactics("all: try (apply sum_to_bound; lia).")]]
int sum_to(int n) {
  int s = 0;
  [[rc::exists("i : Z", "s : Z")]]
  [[rc::inv_vars("i : i @ int<i32>", "s : s @ int<i32>")]]
  [[rc::constraints("{0 ≤ i}", "{i ≤ n}", "{s = i * (i - 1) / 2}")]]
  for (int i = 0; i < n; i++) s += i;
  return s;
}
```

```coq file=lemmas.v
From refinedc.typing Require Import typing.

Lemma sum_to_bound (i : Z) :
  0 ≤ i → i ≤ 46340 → (i * (i - 1)) `div` 2 + i ≤ max_int i32.
Proof.
  intros Hi Hn.
  have Hmul : i * (i - 1) ≤ 46340 * 46339 by nia.
  have Hdiv := Z.div_le_mono (i * (i - 1)) (46340 * 46339) 2 ltac:(lia) Hmul.
  have Hmax : max_int i32 = 2147483647 by vm_compute.
  rewrite Hmax. lia.
Qed.
```
Note: the invariant states the exact partial sum, so the only remaining
obligation is `s + i ≤ max_int i32`, which is nonlinear; the lemma proves it
from `n ≤ 46340`, the real limit below which the sum fits in 32 bits. Inside
Coq, `/` on `Z` prints as `` `div` ``.
