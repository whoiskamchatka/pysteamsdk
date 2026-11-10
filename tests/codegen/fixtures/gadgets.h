// Invented declarations in the shape of the Steamworks SDK headers, for the binding generator's
// tests. Every name and value is made up. gadgets_flat.h declares the flat functions, gadgets.json
// describes them as steam_api.json would, and gadgets.cpp implements them.
#ifndef GADGETS_H
#define GADGETS_H

#if !defined( GADGETS_EXPORTS )
#define S_API extern "C"
#elif defined( _WIN32 )
#define S_API extern "C" __declspec( dllexport )
#else
#define S_API extern "C" __attribute__( ( visibility( "default" ) ) )
#endif

#define GADGETS_INTERFACE_VERSION "Gadgets001"
#define GADGETBOX_INTERFACE_VERSION "GadgetBox001"

typedef signed char int8;
typedef unsigned char uint8;
typedef short int16;
typedef unsigned short uint16;
typedef int int32;
typedef unsigned int uint32;
typedef long long int64;
typedef unsigned long long uint64;
typedef uint32 GadgetHandle_t;
typedef uint64 GadgetCall_t;
typedef char GadgetErrMsg[ 64 ];
typedef void ( *GadgetHook_t )( int32 code );

const int k_cchGadgetName = 32;
const float k_flGadgetScale = 1.5f;

enum EGadgetResult { k_EGadgetResultOK = 1, k_EGadgetResultFail = 2, k_EGadgetResultBusy = 7 };
// The JSON gives All as -1. Every value fits an unsigned int, so Itanium compilers store the enum
// as one and All as 0xffffffff; MSVC stores it as an int, where All is -1.
enum EGadgetColor { k_EGadgetColorRed = 0, k_EGadgetColorBlue = 1, k_EGadgetColorAll = 0xffffffff };

struct GadgetSpot_t { int32 m_nX; int32 m_nY; float m_flZ; };
struct GadgetSize_t { uint16 m_unWidth; uint16 m_unHeight; };
struct GadgetPair_t { uint8 m_ubFirst; uint64 m_ulSecond; };
// Defined in the headers but left out of gadgets.json, as some SDK types are.
struct GadgetMystery_t { int32 m_nValue; };
// Declared and never defined, so only a pointer to it can exist.
struct GadgetTicket;

// Packed to 8 on Windows and 4 elsewhere, as the SDK packs callbacks, so m_ulId moves.
#if defined( _WIN32 )
#pragma pack( push, 8 )
#else
#pragma pack( push, 4 )
#endif
struct GadgetReady_t
{
	enum { k_iCallback = 9101 };
	EGadgetResult m_eResult;
	uint64 m_ulId;
};

// The manual dispatch message and call completion, named and laid out as in the SDK, since
// pysteamsdk's runtime looks for them by name.
struct CallbackMsg_t
{
	int32 m_hSteamUser;
	int m_iCallback;
	uint8 *m_pubParam;
	int m_cubParam;
};

struct SteamAPICallCompleted_t
{
	enum { k_iCallback = 9100 };
	GadgetCall_t m_hAsyncCall;
	int m_iCallback;
	uint32 m_cubParam;
};
#pragma pack( pop )

// Called back from GadgetAPI_IGadgets_Notify, on the thread that calls it.
typedef void ( *GadgetNotice_t )( int32 nCode, const GadgetReady_t *pReady, const char *pchText );

#pragma pack( push, 1 )
// A C++ ID wrapper with private bit-fields, as CSteamID is. Flat functions pass it as a uint64.
class CGadgetID
{
public:
	CGadgetID() { m_id.m_all = 0; }
	uint64 ConvertToUint64() const { return m_id.m_all; }

private:
	union GadgetID_t
	{
		struct GadgetIDParts_t
		{
			uint32 m_unSerial : 32;
			unsigned int m_unBatch : 20;
			unsigned int m_unKind : 4;
			EGadgetColor m_eColor : 8;
		} m_parts;
		uint64 m_all;
	} m_id;
};

struct GadgetAddress_t
{
	void Clear();
	bool IsLoopback() const;

	union
	{
		uint32 m_unIPv4;
		uint8 m_rgubIPv6[ 16 ];
		uint64 m_rgu64[ 2 ];
	};
	uint16 m_usPort;
	bool m_bValid;
};

struct GadgetJoined_t
{
	enum { k_iCallback = 9102 };
	CGadgetID m_gadget;
	uint8 m_ubSeat;
};
#pragma pack( pop )

enum EGadgetKind { k_EGadgetKindInt = 1, k_EGadgetKindText = 2 };

struct GadgetValue_t
{
	EGadgetKind m_eKind;
	union
	{
		int32 m_n;
		int64 m_ll;
		float m_fl;
		const char *m_psz;
		void *m_p;
	} m_val;
	char m_szLabel[ 8 ];
	int16 m_grid[ 2 ][ 3 ];
	GadgetSpot_t m_spot;
	double m_dScale;
	GadgetHook_t m_pfnHook;
	GadgetValue_t *m_pNext;
};

class IGadgetBox
{
public:
	virtual uint32 Count() = 0;
};

// Implemented by the caller and called back later, as the SDK's matchmaking responses are.
class IGadgetListener
{
public:
	virtual void OnDone( int32 code ) = 0;
};

class IGadgets
{
public:
	enum EMode { k_EModeSlow = 0, k_EModeFast = 1 };
};

S_API void GadgetAPI_RunFrame();

#endif
