/* fver shim: <setjmp.h> for the RefinedC front-end. Cerberus's own setjmp.h
 * (which precedes every -I directory) stops with #error, so this file is
 * force-included before each checked file under the same include guard,
 * which turns the later #include <setjmp.h> into a no-op. Prototypes only:
 * a function that calls setjmp/longjmp stays an external call with no spec
 * and can never be verified; every other function in the file can. */
#ifndef _SETJMP_H_
#define _SETJMP_H_
typedef long long jmp_buf[32];
typedef long long sigjmp_buf[34];
int setjmp(jmp_buf env);
int _setjmp(jmp_buf env);
int sigsetjmp(sigjmp_buf env, int savemask);
_Noreturn void longjmp(jmp_buf env, int val);
_Noreturn void _longjmp(jmp_buf env, int val);
_Noreturn void siglongjmp(sigjmp_buf env, int val);
#endif
