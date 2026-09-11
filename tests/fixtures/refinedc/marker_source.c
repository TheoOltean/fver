#include <stddef.h>
#include <refinedc.h>

int __fver_marker(void) { return 0; }
// a comment-only line
#define UNUSED_MACRO 1

/* block comment
   spanning lines */
[[rc::parameters("p : loc", "x : Z")]]
[[rc::args("p @ &shr<x @ int<i32>>")]]
[[rc::returns("void")]]
void set1(int *p) {
  *p = 1;
}
