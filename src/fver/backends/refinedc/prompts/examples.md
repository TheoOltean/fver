# Worked examples

Each example is a complete, accepted submission. Note the pattern: declare
the mathematical variables, give every pointer argument a type that covers
exactly the memory the code touches, return ownership in `rc::ensures`, and
give every loop an invariant with the counter bound.

## Example 1: zero an array

Original:

```c
void zero(int *p, size_t n) {
  for (size_t i = 0; i < n; i++) p[i] = 0;
}
```

Submission:

```c file=function.c
[[rc::parameters("p : loc", "n : nat")]]
[[rc::args("p @ &own<array<int<i32>, {replicate n (uninit (it_layout i32))}>>", "n @ int<size_t>")]]
[[rc::returns("void")]]
[[rc::ensures("own p : array<int<i32>, {replicate n (0 @ int<i32>)}>")]]
void zero(int *p, size_t n) {
  [[rc::exists("i : nat")]]
  [[rc::inv_vars("i : i @ int<size_t>",
                 "p : p @ &own<array<int<i32>, {replicate i (0 @ int<i32>) ++ replicate (n - i) (uninit (it_layout i32))}>>")]]
  [[rc::constraints("{i ≤ n}")]]
  for (size_t i = 0; i < n; i++) p[i] = 0;
}
```
Note: the array type at the loop head is split into the part already written and the part still uninitialised; `{i ≤ n}` keeps `n - i` meaningful.

## Example 2: bounded search, returns an index or -1

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
[[rc::args("p @ &shr<array<int<i32>, {xs `at_type` int<i32>}>>", "n @ int<size_t>", "key @ int<i32>")]]
[[rc::requires("{n = length xs}", "{n ≤ max_int i32}")]]
[[rc::returns("∃ r. r @ int<i32>")]]
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
Note: `&shr` because the array is only read, so no `ensures` for it. `{n ≤ max_int i32}` is what makes the cast `(int)i` safe.

## Example 3: copy between two arrays

Original:

```c
void copy(int *dst, const int *src, size_t n) {
  for (size_t i = 0; i < n; i++) dst[i] = src[i];
}
```

Submission:

```c file=function.c
[[rc::parameters("d : loc", "s : loc", "n : nat", "xs : {list Z}")]]
[[rc::args("d @ &own<array<int<i32>, {replicate n (uninit (it_layout i32))}>>",
           "s @ &shr<array<int<i32>, {xs `at_type` int<i32>}>>",
           "n @ int<size_t>")]]
[[rc::requires("{n = length xs}")]]
[[rc::returns("void")]]
[[rc::ensures("own d : array<int<i32>, {xs `at_type` int<i32>}>")]]
void copy(int *dst, const int *src, size_t n) {
  [[rc::exists("i : nat")]]
  [[rc::inv_vars("i : i @ int<size_t>",
                 "dst : d @ &own<array<int<i32>, {(take i xs) `at_type` int<i32> ++ replicate (n - i) (uninit (it_layout i32))}>>")]]
  [[rc::constraints("{i ≤ n}")]]
  for (size_t i = 0; i < n; i++) dst[i] = src[i];
}
```
Note: two pointers, two ownership descriptions. The source is shared and unchanged; the destination's type tracks how much has been copied.

## Example 4: a struct with a length field

Original:

```c
struct buf { int *data; size_t len; };

int buf_last(struct buf *b) {
  if (b->len == 0) return 0;
  return b->data[b->len - 1];
}
```

Submission (struct annotations are part of the function's file; include them only if the struct is not already annotated):

```c file=function.c
[[rc::parameters("b : loc", "n : nat", "xs : {list Z}")]]
[[rc::args("b @ &own<struct<struct_buf, &own<array<int<i32>, {xs `at_type` int<i32>}>>, n @ int<size_t>>>")]]
[[rc::requires("{n = length xs}")]]
[[rc::returns("∃ r. r @ int<i32>")]]
[[rc::ensures("own b : struct<struct_buf, &own<array<int<i32>, {xs `at_type` int<i32>}>>, n @ int<size_t>>")]]
int buf_last(struct buf *b) {
  if (b->len == 0) return 0;
  return b->data[b->len - 1];
}
```
Note: the struct type lists field types in declaration order. The subtraction `b->len - 1` is on `size_t`, so the only obligation is the bound `n - 1 < length xs`, which follows from `n = length xs` and the guard.

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
[[rc::returns("∃ r. r @ int<i32>")]]
[[rc::lemmas("sum_to_bound")]]
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
From Coq Require Import ZArith Lia.
Open Scope Z_scope.

Lemma sum_to_bound (i n : Z) :
  0 <= i -> i <= n -> n <= 46340 -> i * (i - 1) / 2 + i <= 2147483647.
Proof.
  intros. nia.
Qed.
```
Note: the invariant states the exact partial sum so the overflow obligation `s + i ≤ max_int i32` reduces to arithmetic; `nia` needs the helper because the bound is nonlinear. The precondition `n ≤ 46340` is the real limit below which the sum fits in 32 bits.
