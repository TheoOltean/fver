/* fver shim: minimal POSIX <unistd.h> (prototypes only, no bodies). */
#ifndef _FVER_UNISTD_H
#define _FVER_UNISTD_H
#include <stddef.h>
#include <sys/types.h>
#define STDIN_FILENO 0
#define STDOUT_FILENO 1
#define STDERR_FILENO 2
#define SEEK_SET 0
#define SEEK_CUR 1
#define SEEK_END 2
#define F_OK 0
#define R_OK 4
#define W_OK 2
#define X_OK 1
ssize_t read(int fd, void *buf, size_t n);
ssize_t write(int fd, const void *buf, size_t n);
int close(int fd);
off_t lseek(int fd, off_t off, int whence);
int unlink(const char *path);
int rmdir(const char *path);
int access(const char *path, int mode);
int dup(int fd);
int dup2(int fd, int fd2);
int pipe(int fds[2]);
pid_t fork(void);
pid_t getpid(void);
pid_t getppid(void);
uid_t getuid(void);
gid_t getgid(void);
int isatty(int fd);
unsigned int sleep(unsigned int seconds);
int usleep(useconds_t usec);
char *getcwd(char *buf, size_t size);
int chdir(const char *path);
int ftruncate(int fd, off_t length);
int fsync(int fd);
long sysconf(int name);
int execv(const char *path, char *const argv[]);
int execvp(const char *file, char *const argv[]);
void _exit(int status);
#endif
