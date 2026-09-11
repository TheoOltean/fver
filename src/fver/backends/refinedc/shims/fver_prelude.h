/* fver prelude, force-included (`--include=`) before every file RefinedC
 * checks. It neutralises GNU extensions that Cerberus's parser rejects and
 * that carry no meaning for verification. Nothing here changes semantics:
 * attributes only affect code generation, `__extension__` only silences
 * pedantic warnings. RefinedC's own annotations use C2x `[[rc::...]]`
 * attributes, which are unaffected. */
#ifndef _FVER_PRELUDE_H
#define _FVER_PRELUDE_H
#define __attribute__(x)
#define __attribute(x)
#define __extension__
#define __restrict restrict
#define __restrict__ restrict
#define __inline inline
#define __inline__ inline
#define __builtin_expect(expr, expected) (expr)
#endif
