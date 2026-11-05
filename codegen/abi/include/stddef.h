/* Part of pysteamsdk's codegen. Every type comes from the compiler's predefined macros for the
   target being laid out, so no host header is involved. */
#ifndef PYSTEAMSDK_STDDEF_H
#define PYSTEAMSDK_STDDEF_H

typedef __SIZE_TYPE__ size_t;
typedef __PTRDIFF_TYPE__ ptrdiff_t;

#define NULL 0
#define offsetof( type, member ) __builtin_offsetof( type, member )

#endif
