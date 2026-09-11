# POSIX shim headers for the RefinedC front-end

Cerberus ships its own ISO C libc headers and has no POSIX headers, so any
file that includes `<unistd.h>`, `<sys/types.h>`, ... is rejected before a
single function is looked at. These stubs make such files *parse*: they
declare the common types and prototypes only. There are no bodies, and no
specifications: every function declared here is an external call and stays
a trusted assumption in the ledger unless a spec is supplied under
`.fver/external/`.

They are added with `-I` after the project's own include directories, so a
project that ships a real header of the same name wins.
