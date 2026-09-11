/* fver shim: minimal POSIX <strings.h> (prototypes only). */
#ifndef _FVER_STRINGS_H
#define _FVER_STRINGS_H
#include <stddef.h>
int strcasecmp(const char *a, const char *b);
int strncasecmp(const char *a, const char *b, size_t n);
#endif
