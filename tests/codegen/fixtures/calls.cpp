// The functions tests/codegen/call_child.py calls through ctypes. Every value is invented.
#include "calls.h"

#if defined( _WIN32 )
#define EXPORT extern "C" __declspec( dllexport )
// MSVC warns that a C function returns a C++ class; that is the case being tested.
#pragma warning( disable : 4190 )
#else
#define EXPORT extern "C" __attribute__( ( visibility( "default" ) ) )
#endif

EXPORT uint64_t TakeLocation( Location location ) { return location.id * 16 + location.kind; }

EXPORT Stick ReturnStick()
{
	Stick stick = { kKindMore, 1.5f, -2.25f, true };
	return stick;
}

EXPORT Motion ReturnMotion()
{
	Motion motion = { 1, 2, 3, 4, 5, 6, 7, 8, 9, 10 };
	return motion;
}

EXPORT Button ReturnButton()
{
	Button button = { true, false };
	return button;
}

EXPORT Address ReturnAddress()
{
	Address address;
	for ( int i = 0; i < 16; ++i )
		address.v6[i] = (uint8_t)( i + 1 );
	address.kind = kKindMore;
	return address;
}

EXPORT uint64_t TakeAddress( Address address )
{
	return address.words[0] ^ ( address.words[1] << 1 ) ^ address.kind;
}

EXPORT Id ReturnId() { return Id( 0x0110000100000042ull ); }

EXPORT uint64_t TakeId( Id id ) { return id.Value(); }

EXPORT Game ReturnGame() { return Game( 0x1234 ); }

EXPORT uint64_t TakeGame( Game game ) { return game.Value(); }
