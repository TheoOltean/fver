#include "../util.h"
#include <string.h>
#include <stdlib.h>

static int helper(int x) {
    return x + 1;   // shadows util.c's static helper
}

int parse_header(const unsigned char *buf, size_t len, int *out) {
    unsigned char tmp[16];
    int acc = 0;
    if (len > sizeof tmp) return -1;
    memcpy(tmp, buf, len);
    for (size_t i = 0; i < len; i++) {
        acc += tmp[i];
    }
    *out = helper(acc) + add(acc, 1);
    return 0;
}
