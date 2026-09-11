/* fver shim: minimal POSIX <fcntl.h> (prototypes only). */
#ifndef _FVER_FCNTL_H
#define _FVER_FCNTL_H
#include <sys/types.h>
#define O_RDONLY 0
#define O_WRONLY 1
#define O_RDWR 2
#define O_CREAT 0100
#define O_EXCL 0200
#define O_TRUNC 01000
#define O_APPEND 02000
#define O_NONBLOCK 04000
#define O_CLOEXEC 02000000
#define F_GETFL 3
#define F_SETFL 4
#define F_GETFD 1
#define F_SETFD 2
#define FD_CLOEXEC 1
int open(const char *path, int flags, ...);
int creat(const char *path, mode_t mode);
int fcntl(int fd, int cmd, ...);
#endif
