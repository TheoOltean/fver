#include "util.h"
#include <string.h>

static int helper(int x) {
    return x * 2;
}

int add(int a, int b) {
    /* trivial */
    return helper(a) + b;
}

int get_value(void) { return 42; }
