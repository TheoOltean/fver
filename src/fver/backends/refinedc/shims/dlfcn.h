/* fver shim: minimal POSIX <dlfcn.h> (prototypes only). */
#ifndef _FVER_DLFCN_H
#define _FVER_DLFCN_H
#define RTLD_LAZY 1
#define RTLD_NOW 2
#define RTLD_GLOBAL 0x100
#define RTLD_LOCAL 0
void *dlopen(const char *file, int mode);
void *dlsym(void *handle, const char *name);
int dlclose(void *handle);
char *dlerror(void);
#endif
