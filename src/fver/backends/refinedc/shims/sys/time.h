/* fver shim: minimal POSIX <sys/time.h> (prototypes only). */
#ifndef _FVER_SYS_TIME_H
#define _FVER_SYS_TIME_H
#include <sys/types.h>
struct timeval { time_t tv_sec; suseconds_t tv_usec; };
struct timezone { int tz_minuteswest; int tz_dsttime; };
int gettimeofday(struct timeval *tv, void *tz);
int settimeofday(const struct timeval *tv, const struct timezone *tz);
#endif
