// Invented records shaped like the ones the Steamworks flat API passes by value, and like its
// C++ ID wrappers, to see how ctypes passes each. calls.cpp exports a function for each case.
#ifndef CALLS_H
#define CALLS_H

#include <stdint.h>

enum Kind { kKindNone, kKindSome, kKindMore };

// Packed to 8 on Windows and 4 elsewhere, as SDK callbacks are, which leaves id misaligned.
#if defined( _WIN32 )
#pragma pack( push, 8 )
#else
#pragma pack( push, 4 )
#endif
struct Location { Kind kind; uint64_t id; };
#pragma pack( pop )

#pragma pack( push, 1 )
struct Stick { Kind mode; float x; float y; bool active; };
struct Motion { float qx, qy, qz, qw, ax, ay, az, vx, vy, vz; };
struct Button { bool state; bool active; };
struct Address
{
	union { uint32_t v4; uint8_t v6[16]; uint64_t words[2]; };
	Kind kind;
};

// Trivially copyable, but its constructors and private members make it no aggregate.
class Id
{
public:
	Id() : m_bits() {}
	explicit Id( uint64_t value ) { m_bits.all = value; }
	uint64_t Value() const { return m_bits.all; }

private:
	union Bits_t
	{
		struct { uint32_t account : 32; unsigned int instance : 20; unsigned int type : 4; unsigned int universe : 8; } parts;
		uint64_t all;
	} m_bits;
};

// Its own copy constructor makes it non-trivial for calls.
class Game
{
public:
	Game() : m_value( 0 ) {}
	explicit Game( uint64_t value ) : m_value( value ) {}
	Game( const Game &that ) : m_value( that.m_value ) {}
	Game &operator=( const Game &that ) { m_value = that.m_value; return *this; }
	uint64_t Value() const { return m_value; }

private:
	uint64_t m_value;
};
#pragma pack( pop )

#endif
