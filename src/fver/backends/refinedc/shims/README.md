# POSIX shim headers for the RefinedC front-end

Cerberus ships its own ISO C libc headers and has no POSIX headers, so any
file that includes `<unistd.h>`, `<sys/types.h>`, ... is rejected before a
single function is looked at. These stubs make such files *parse*: they
declare the common types and prototypes only. There are no bodies, and no
specifications: every function declared here is an external call and stays
a trusted assumption in the ledger unless a spec is supplied under
`.fver/external/`.

They are always added with `-I` after the project's own include directories,
so a project that ships a real header of the same name wins.

`setjmp.h` is special: Cerberus ships its own, which precedes every `-I`
directory and ends in `#error`. The shim is therefore force-included
(`--include=`) before each checked file under Cerberus's include guard, so the
later `#include <setjmp.h>` is a no-op.

Floating point is handled elsewhere: see `../opaque.py`. The copy of the code
RefinedC checks has `float`/`double`/`long double` replaced by same-size
structs (`struct fver_f32/f64/fld`, defined in a generated `fver_opaque.h`
that is also force-included), and project headers are mirrored with the same
rewrite under `<workspace>/shadow/` ahead of the real directories.
