#ifndef UTIL_H
#define UTIL_H
#include <stddef.h>
int add(int a, int b);
int parse_header(const unsigned char *buf, size_t len, int *out);
int get_value(void);
#endif
