// Invented declarations covering the layout rules codegen.abi must get right on every target.
#ifndef LAYOUTS_H
#define LAYOUTS_H

#include <stddef.h>
#include <stdint.h>

struct Plain { char c; int i; double d; };

// long is 4 bytes on Windows and 8 on Linux and macOS.
struct WithLong { char c; long l; };

#pragma pack( push, 4 )
struct Packed4 { uint32_t a; uint64_t b; };
#pragma pack( pop )

#pragma pack( push, 1 )
struct Packed1 { uint8_t a; uint32_t b; uint16_t c; };
#pragma pack( pop )

union Value { int32_t i; int64_t l; float f; const char *s; void *p; };

struct Tagged
{
	int kind;
	union { int32_t i; double d; };
};

struct Named
{
	struct { short x, y; } point;
	int z;
};

struct Outer
{
	struct Inner { char a[3]; } inner[2];
	float grid[2][3];
};

// MSVC starts a new storage unit where the declared type's size changes; GCC and Clang do not.
struct Bits
{
	unsigned int a : 3;
	unsigned int b : 30;
	unsigned char c : 2;
	unsigned int : 0;
	unsigned int d : 1;
};

struct Aligned { alignas( 16 ) float v[4]; char tag; };

struct Forward;

struct Polymorphic { virtual ~Polymorphic(); int x; };

struct Derived : Plain { int extra; };

struct Pointers
{
	const char *name;
	void *data;
	void ( *callback )( int );
	struct Forward *forward;
	Value values[2];
	size_t count;
};

enum Small { kSmallA = 1 };

// Itanium C++ stores this in unsigned int; MSVC keeps int.
enum Wide { kWideA = 0x80000000u };

enum Fixed : uint8_t { kFixedA = 1 };

enum class Scoped : int64_t { kScopedA = 1 };

typedef long platform_long;
typedef void ( *callback_t )( int );
typedef struct { int handle; } handle_t;

#endif
