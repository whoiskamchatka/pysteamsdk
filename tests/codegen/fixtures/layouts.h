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

// A packed class that keeps its bits private, in a nested union, as ID wrappers do.
#pragma pack( push, 1 )
class Handle
{
public:
	Handle() : m_bits() {}
	uint64_t Value() const { return m_bits.all; }

private:
	enum Domain { kDomainNone, kDomainLocal, kDomainRemote };
	union Bits_t
	{
		struct Parts_t
		{
			uint32_t id : 32;
			unsigned int slot : 20;
			unsigned int kind : 4;
			Domain domain : 8;
		} parts;
		uint64_t all;
	} m_bits;
	uint16_t m_tag;
};
#pragma pack( pop )

// Mixed access makes this non-standard-layout; offsetof still works on it with these compilers.
struct Shown { int shown; private: int hidden; public: char last; };

typedef int32_t Count;
typedef Count Total;
struct Totals { Total total; Count counts[4]; const Count *first; };

struct Flags { bool on : 1; int level : 3; signed char delta : 4; Fixed fixed : 4; };

// Members reached through anonymous and unnamed records, bit-fields among them.
struct Mixed
{
	int head;
	struct { unsigned int lo : 4, hi : 4; };
	union { float f; int32_t i; struct { int16_t a, b; }; };
	struct { unsigned int a : 2; char tag; } nib[2];
	union { int i; float f; } slot[2];
	char tail;
};

struct Event { Small small; Fixed fixed; Scoped scoped; Wide wide; };

// One name, a different record on each platform.
#ifdef _WIN32
struct Native { void *handle; uint32_t flags; };
#else
struct Native { int fd; uint64_t flags; };
#endif

// Packed to 8 on Windows and 4 elsewhere, as SDK callbacks are.
#if defined( _WIN32 )
#pragma pack( push, 8 )
#else
#pragma pack( push, 4 )
#endif
struct Notice { uint32_t code; uint64_t stamp; };
#pragma pack( pop )

// long double and wchar_t change size between platforms.
struct Extended { char c; long double x; wchar_t w; char16_t u; };

typedef struct Forward Forward_t;
typedef struct Forward *ForwardRef;

#endif
