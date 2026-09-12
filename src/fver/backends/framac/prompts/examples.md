# Worked examples

Every submission below was accepted by Frama-C/WP as written.

## Example 1: copy a buffer

Original:

```c
void copy(char *dst, const char *src, size_t n) {
  size_t i;
  for (i = 0; i < n; i++) dst[i] = src[i];
}
```

Submission:

```c file=function.c
/*@ requires \valid(dst + (0 .. n-1));
    requires \valid_read(src + (0 .. n-1));
    requires \separated(dst + (0 .. n-1), src + (0 .. n-1));
    assigns dst[0 .. n-1];
    ensures \forall integer k; 0 <= k < n ==> dst[k] == src[k];
*/
void copy(char *dst, const char *src, size_t n) {
  size_t i;
  /*@ loop invariant 0 <= i <= n;
      loop invariant \forall integer k; 0 <= k < i ==> dst[k] == src[k];
      loop assigns i, dst[0 .. n-1];
  */
  for (i = 0; i < n; i++) dst[i] = src[i];
}
```

## Example 2: a struct with an array and a count

Original:

```c
struct vec { int *data; int len; int cap; };

int vec_get(const struct vec *v, int i) {
  if (i < 0 || i >= v->len) return -1;
  return v->data[i];
}
```

Submission:

```c file=function.c
/*@ requires \valid_read(v);
    requires 0 <= v->len;
    requires \valid_read(v->data + (0 .. v->len - 1));
    assigns \nothing;
*/
int vec_get(const struct vec *v, int i) {
  if (i < 0 || i >= v->len) return -1;
  return v->data[i];
}
```

## Example 3: a string scan (WP knows strlen from <string.h>)

Original:

```c
static int count_char(const char *s, char c) {
  int n = 0;
  while (*s) {
    if (*s == c) n++;
    s++;
  }
  return n;
}
```

Submission:

```c file=function.c
/*@ requires valid_read_string(s);
    requires strlen(s) <= INT_MAX;
    assigns \nothing;
    ensures 0 <= \result <= strlen(\old(s));
*/
static int count_char(const char *s, char c) {
  int n = 0;
  /*@ loop invariant \at(s, Pre) <= s <= \at(s, Pre) + strlen(\at(s, Pre));
      loop invariant 0 <= n <= s - \at(s, Pre);
      loop invariant strlen(s) == strlen(\at(s, Pre)) - (s - \at(s, Pre));
      loop assigns s, n;
  */
  while (*s) {
    if (*s == c) n++;
    s++;
  }
  return n;
}
```

## Example 4: a caller relying on a callee's contract

The callee `vec_get` above has an accepted contract, shown in the task as a
prototype with its ACSL block. The caller only needs to establish the
callee's `requires`:

```c file=function.c
/*@ requires \valid_read(v);
    requires 0 <= v->len;
    requires \valid_read(v->data + (0 .. v->len - 1));
    assigns \nothing;
*/
int vec_first(const struct vec *v) {
  return vec_get(v, 0);
}
```

## Example 5: allocation (malloc is specified by Frama-C's libc)

```c file=function.c
/*@ assigns \nothing;
    ensures \result == \null || \valid(\result);
*/
static struct node *node_new(void) {
  struct node *n = (struct node *)malloc(sizeof(struct node));
  if (n == NULL) return NULL;
  n->next = NULL;
  n->value = 0;
  return n;
}
```

Note the `assigns \nothing`: fresh memory from `malloc` is not a
pre-existing location, so writes to it are not listed.
