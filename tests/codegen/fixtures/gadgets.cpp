// Implements gadgets_flat.h for the binding generator's tests, plus stand-ins for the functions
// pysteamsdk's loader requires, so the lifecycle can load this library as it would steam_api.
// Every name and value is invented.
#define GADGETS_EXPORTS
#include "gadgets_flat.h"

#include <string.h>

#include <deque>
#include <map>
#include <vector>

#if defined( _WIN32 )
#define TEST_API extern "C" __declspec( dllexport )
#else
#define TEST_API extern "C" __attribute__( ( visibility( "default" ) ) )
#endif

namespace
{
const uint64 kOwner = 0x0110000100000042ull;
const char kName[] = "gadget";

class Gadgets : public IGadgets
{
};

class Box : public IGadgetBox
{
public:
	uint32 Count() { return 3; }
};

Gadgets g_gadgets;
Box g_box;
bool g_initialized = false;
uint32 g_calls = 0;

// Counts every call that reaches this library, so a test can tell that a refused one did not.
bool Valid( IGadgets *self )
{
	++g_calls;
	return self == &g_gadgets;
}

// Manual dispatch as steam_api.h describes it: callbacks queue until fetched one at a time, and a
// finished call's result waits until SteamAPI_ManualDispatch_GetAPICallResult takes it.
struct Message
{
	int id;
	std::vector<uint8> data;
	bool failed;
};

std::deque<Message> g_queue;
std::map<uint64, Message> g_results;
uint8 *g_current = 0;
int g_currentSize = 0;
bool g_manual = false;
GadgetCall_t g_lastCall = 0x1000;
GadgetNotice_t g_notice = 0;
// What Notify passes its callback, overwritten once the callback returns.
GadgetReady_t g_noticeReady;
char g_noticeText[ 16 ];

template < class T > Message Make( int id, const T &value, bool failed = false )
{
	Message message;
	message.id = id;
	message.data.assign( (const uint8 *)&value, (const uint8 *)&value + sizeof( T ) );
	message.failed = failed;
	return message;
}
}

TEST_API int SteamAPI_InitFlat( char *pOutErrMsg )
{
	if ( pOutErrMsg )
		pOutErrMsg[ 0 ] = 0;
	g_initialized = true;
	return 0;
}

TEST_API void SteamAPI_Shutdown() { g_initialized = false; }
TEST_API int32 SteamAPI_GetHSteamPipe() { return 1; }
TEST_API int32 SteamAPI_GetHSteamUser() { return 2; }
TEST_API void SteamAPI_ManualDispatch_Init() { g_manual = true; }
TEST_API void SteamAPI_ManualDispatch_RunFrame( int32 ) {}

TEST_API bool SteamAPI_ManualDispatch_GetNextCallback( int32 pipe, CallbackMsg_t *pCallbackMsg )
{
	// The last message must be freed first.
	if ( !g_manual || pipe != 1 || g_current || g_queue.empty() )
		return false;
	Message next = g_queue.front();
	g_queue.pop_front();
	g_currentSize = (int)next.data.size();
	g_current = new uint8[ g_currentSize + 1 ];
	memcpy( g_current, next.data.data(), next.data.size() );
	pCallbackMsg->m_hSteamUser = 2;
	pCallbackMsg->m_iCallback = next.id;
	pCallbackMsg->m_pubParam = g_current;
	pCallbackMsg->m_cubParam = g_currentSize;
	return true;
}

TEST_API void SteamAPI_ManualDispatch_FreeLastCallback( int32 )
{
	if ( !g_current )
		return;
	memset( g_current, 0xdd, g_currentSize );
	delete[] g_current;
	g_current = 0;
}

TEST_API bool SteamAPI_ManualDispatch_GetAPICallResult( int32 pipe, uint64 hCall, void *pCallback, int cubCallback, int iCallbackExpected, bool *pbFailed )
{
	std::map<uint64, Message>::iterator found = g_results.find( hCall );
	if ( pipe != 1 || found == g_results.end() || found->second.id != iCallbackExpected || (int)found->second.data.size() != cubCallback )
		return false;
	memcpy( pCallback, found->second.data.data(), cubCallback );
	*pbFailed = found->second.failed;
	g_results.erase( found );
	return true;
}

TEST_API uint32 GadgetTest_Calls() { return g_calls; }
TEST_API uint32 GadgetTest_Results() { return (uint32)g_results.size(); }

// A callback whose size no declaration matches, as from a Steam client newer than the SDK.
TEST_API void GadgetTest_PostOdd()
{
	uint8 odd[ 3 ] = { 1, 2, 3 };
	g_queue.push_back( Make( GadgetJoined_t::k_iCallback, odd ) );
}

S_API void GadgetAPI_RunFrame() {}

S_API IGadgets *GadgetAPI_Gadgets_v001() { return g_initialized ? &g_gadgets : 0; }
S_API int8 GadgetAPI_IGadgets_Echo8( IGadgets *self, int8 nValue ) { return Valid( self ) ? nValue : 0; }
S_API uint8 GadgetAPI_IGadgets_EchoU8( IGadgets *self, uint8 unValue ) { return Valid( self ) ? unValue : 0; }
S_API int16 GadgetAPI_IGadgets_Echo16( IGadgets *self, int16 nValue ) { return Valid( self ) ? nValue : 0; }
S_API uint16 GadgetAPI_IGadgets_EchoU16( IGadgets *self, uint16 unValue ) { return Valid( self ) ? unValue : 0; }
S_API int32 GadgetAPI_IGadgets_Echo32( IGadgets *self, int32 nValue ) { return Valid( self ) ? nValue : 0; }
S_API uint32 GadgetAPI_IGadgets_EchoU32( IGadgets *self, uint32 unValue ) { return Valid( self ) ? unValue : 0; }
S_API int64 GadgetAPI_IGadgets_Echo64( IGadgets *self, int64 nValue ) { return Valid( self ) ? nValue : 0; }
S_API uint64 GadgetAPI_IGadgets_EchoU64( IGadgets *self, uint64 unValue ) { return Valid( self ) ? unValue : 0; }
S_API float GadgetAPI_IGadgets_EchoFloat( IGadgets *self, float flValue ) { return Valid( self ) ? flValue : 0; }
S_API double GadgetAPI_IGadgets_EchoDouble( IGadgets *self, double dValue ) { return Valid( self ) ? dValue : 0; }
S_API long GadgetAPI_IGadgets_EchoLong( IGadgets *self, long nValue ) { return Valid( self ) ? nValue : 0; }
S_API bool GadgetAPI_IGadgets_Invert( IGadgets *self, bool bValue ) { return Valid( self ) && !bValue; }

// Folds every argument into one number, so a test can tell that each arrived in its place.
S_API uint64 GadgetAPI_IGadgets_Mix( IGadgets *self, int8 a, uint16 b, int32 c, uint64 d, float e, double f, bool g, EGadgetColor h )
{
	if ( !Valid( self ) )
		return 0;
	uint64 mixed = (uint64)(int64)a;
	mixed = mixed * 31 + b;
	mixed = mixed * 31 + (uint64)(int64)c;
	mixed = mixed * 31 + d;
	mixed = mixed * 31 + (uint64)(int64)( e * 4 );
	mixed = mixed * 31 + (uint64)(int64)( f * 8 );
	mixed = mixed * 31 + ( g ? 1 : 0 );
	return mixed * 31 + (uint32)h;
}

S_API EGadgetResult GadgetAPI_IGadgets_Classify( IGadgets *self, int32 nValue )
{
	if ( !Valid( self ) || nValue == 0 )
		return k_EGadgetResultBusy;
	return nValue > 0 ? k_EGadgetResultOK : k_EGadgetResultFail;
}

S_API EGadgetColor GadgetAPI_IGadgets_GetColorAll( IGadgets *self )
{
	Valid( self );
	return k_EGadgetColorAll;
}

S_API int32 GadgetAPI_IGadgets_SetMode( IGadgets *self, IGadgets::EMode eMode ) { return Valid( self ) ? eMode * 10 + 1 : 0; }

S_API uint32 GadgetAPI_IGadgets_Length( IGadgets *self, const char *pchText )
{
	if ( !Valid( self ) || !pchText )
		return 0xffffffff;
	return (uint32)strlen( pchText );
}

// Like the SDK's out_string_count functions: a null buffer asks for the size needed.
S_API int32 GadgetAPI_IGadgets_FillName( IGadgets *self, char *pchBuffer, int32 cchBuffer )
{
	if ( !Valid( self ) )
		return -1;
	if ( !pchBuffer || cchBuffer <= 0 )
		return (int32)sizeof( kName );
	int32 count = cchBuffer - 1 < (int32)strlen( kName ) ? cchBuffer - 1 : (int32)strlen( kName );
	memcpy( pchBuffer, kName, count );
	pchBuffer[ count ] = 0;
	return count;
}

S_API const char *GadgetAPI_IGadgets_GetName( IGadgets *self ) { return Valid( self ) ? kName : 0; }

S_API const char *GadgetAPI_IGadgets_GetNoName( IGadgets *self )
{
	Valid( self );
	return 0;
}

S_API uint32 GadgetAPI_IGadgets_Checksum( IGadgets *self, const void *pData, uint32 cbData )
{
	if ( !Valid( self ) || !pData )
		return 0xffffffff;
	uint32 sum = 0;
	for ( uint32 i = 0; i < cbData; ++i )
		sum = sum * 33 + ( (const uint8 *)pData )[ i ];
	return sum;
}

S_API uint32 GadgetAPI_IGadgets_Scribble( IGadgets *self, void *pData, uint32 cbData )
{
	if ( !Valid( self ) || !pData )
		return 0;
	for ( uint32 i = 0; i < cbData; ++i )
		( (uint8 *)pData )[ i ] = (uint8)( i * 3 + 1 );
	return cbData;
}

S_API bool GadgetAPI_IGadgets_GetCount( IGadgets *self, int32 *pnCount )
{
	if ( !Valid( self ) || !pnCount )
		return false;
	*pnCount = 42;
	return true;
}

S_API bool GadgetAPI_IGadgets_Locate( IGadgets *self, GadgetSpot_t *pSpot )
{
	if ( !Valid( self ) || !pSpot )
		return false;
	pSpot->m_nX = 3;
	pSpot->m_nY = -4;
	pSpot->m_flZ = 0.5f;
	return true;
}

S_API int64 GadgetAPI_IGadgets_Distance( IGadgets *self, const GadgetSpot_t &from, const GadgetSpot_t &to )
{
	if ( !Valid( self ) )
		return -1;
	int64 dx = (int64)to.m_nX - from.m_nX, dy = (int64)to.m_nY - from.m_nY;
	return ( dx < 0 ? -dx : dx ) + ( dy < 0 ? -dy : dy );
}

S_API bool GadgetAPI_IGadgets_Explain( IGadgets *self, GadgetErrMsg &errMsg )
{
	if ( !Valid( self ) )
		return false;
	memset( errMsg, 0, sizeof( errMsg ) );
	memcpy( errMsg, "explained", 9 );
	return true;
}

S_API GadgetSpot_t GadgetAPI_IGadgets_GetSpot( IGadgets *self )
{
	GadgetSpot_t spot = { 7, -8, 2.5f };
	if ( !Valid( self ) )
		spot.m_nX = 0;
	return spot;
}

S_API int64 GadgetAPI_IGadgets_SpotSum( IGadgets *self, GadgetSpot_t spot )
{
	return Valid( self ) ? (int64)spot.m_nX * 1000 + spot.m_nY + (int64)( spot.m_flZ * 2 ) : 0;
}

S_API uint32 GadgetAPI_IGadgets_Area( IGadgets *self, GadgetSize_t size ) { return Valid( self ) ? size.m_unWidth * size.m_unHeight : 0; }
S_API uint64 GadgetAPI_IGadgets_PairSum( IGadgets *self, GadgetPair_t pair ) { return Valid( self ) ? pair.m_ubFirst + pair.m_ulSecond : 0; }

S_API void GadgetAPI_IGadgets_SetHook( IGadgets *self, GadgetHook_t pfnHook )
{
	if ( Valid( self ) && pfnHook )
		pfnHook( 1 );
}

S_API void GadgetAPI_IGadgets_Listen( IGadgets *self, IGadgetListener *pListener )
{
	if ( Valid( self ) && pListener )
		pListener->OnDone( 2 );
}

S_API bool GadgetAPI_IGadgets_ReadTicket( IGadgets *self, GadgetTicket *pTicket ) { return Valid( self ) && pTicket; }
S_API bool GadgetAPI_IGadgets_Inspect( IGadgets *self, GadgetMystery_t *pMystery ) { return Valid( self ) && pMystery; }
S_API IGadgetBox *GadgetAPI_IGadgets_OpenBox( IGadgets *self ) { return Valid( self ) ? &g_box : 0; }
// Completes at once, with GadgetReady_t, and fails for gadget 0.
S_API GadgetCall_t GadgetAPI_IGadgets_Fetch( IGadgets *self, uint64_gadgetid gadgetID )
{
	if ( !Valid( self ) )
		return 0;
	GadgetCall_t call = ++g_lastCall;
	GadgetReady_t ready;
	ready.m_eResult = gadgetID ? k_EGadgetResultOK : k_EGadgetResultFail;
	ready.m_ulId = gadgetID ^ 0x5a5a;
	g_results[ call ] = Make( GadgetReady_t::k_iCallback, ready, gadgetID == 0 );
	SteamAPICallCompleted_t completed;
	completed.m_hAsyncCall = call;
	completed.m_iCallback = GadgetReady_t::k_iCallback;
	completed.m_cubParam = sizeof( GadgetReady_t );
	g_queue.push_back( Make( SteamAPICallCompleted_t::k_iCallback, completed ) );
	return call;
}
S_API uint64_gadgetid GadgetAPI_IGadgets_GetOwner( IGadgets *self ) { return Valid( self ) ? kOwner : 0; }

S_API bool GadgetAPI_IGadgets_GetOwnerInto( IGadgets *self, CGadgetID *pOwner )
{
	if ( !Valid( self ) || !pOwner )
		return false;
	memcpy( (void *)pOwner, &kOwner, sizeof( kOwner ) );
	return true;
}

S_API bool GadgetAPI_IGadgets_Poll( IGadgets *self, GadgetReady_t *pReady )
{
	if ( !Valid( self ) || !pReady )
		return false;
	pReady->m_eResult = k_EGadgetResultFail;
	pReady->m_ulId = 0x123456789abcdef0ull;
	return true;
}

S_API bool GadgetAPI_IGadgets_Join( IGadgets *self, GadgetJoined_t *pJoined )
{
	if ( !Valid( self ) || !pJoined )
		return false;
	memcpy( (void *)&pJoined->m_gadget, &kOwner, sizeof( kOwner ) );
	pJoined->m_ubSeat = 5;
	g_queue.push_back( Make( GadgetJoined_t::k_iCallback, *pJoined ) );
	return true;
}

S_API bool GadgetAPI_IGadgets_Describe( IGadgets *self, GadgetValue_t *pValue )
{
	if ( !Valid( self ) || !pValue )
		return false;
	pValue->m_eKind = k_EGadgetKindText;
	pValue->m_val.m_psz = kName;
	memcpy( pValue->m_szLabel, "label", 6 );
	for ( int i = 0; i < 2; ++i )
		for ( int j = 0; j < 3; ++j )
			pValue->m_grid[ i ][ j ] = (int16)( i * 10 + j - 5 );
	pValue->m_spot.m_nX = 1;
	pValue->m_spot.m_nY = 2;
	pValue->m_spot.m_flZ = 3.0f;
	pValue->m_dScale = 0.25;
	pValue->m_pfnHook = 0;
	pValue->m_pNext = pValue;
	return true;
}

S_API bool GadgetAPI_IGadgets_GetAddress( IGadgets *self, GadgetAddress_t *pAddress )
{
	if ( !Valid( self ) || !pAddress )
		return false;
	for ( int i = 0; i < 16; ++i )
		pAddress->m_rgubIPv6[ i ] = (uint8)( 0x10 + i );
	pAddress->m_usPort = 4242;
	pAddress->m_bValid = true;
	return true;
}

S_API int32 GadgetAPI_IGadgets_CountNames( IGadgets *self, const char **ppchNames, int32 nNames )
{
	if ( !Valid( self ) || !ppchNames )
		return -1;
	int32 total = 0;
	for ( int32 i = 0; i < nNames; ++i )
		total += ppchNames[ i ] ? (int32)strlen( ppchNames[ i ] ) : 100;
	return total;
}

S_API IGadgetBox *GadgetAPI_GadgetBox_v001() { return &g_box; }
S_API IGadgetBox *GadgetAPI_GameServerGadgetBox_v001() { return 0; }
S_API uint32 GadgetAPI_IGadgetBox_Count( IGadgetBox *self ) { return self->Count(); }
S_API void GadgetAPI_IGadgetListener_OnDone( IGadgetListener *self, int32 code ) { self->OnDone( code ); }

S_API void GadgetAPI_GadgetAddress_t_Clear( GadgetAddress_t *self ) { memset( (void *)self, 0, sizeof( *self ) ); }
S_API bool GadgetAPI_GadgetAddress_t_IsLoopback( GadgetAddress_t *self ) { return self->m_unIPv4 == 0x0100007f; }

S_API void GadgetAPI_IGadgets_SetNotice( IGadgets *self, GadgetNotice_t pfnNotice )
{
	if ( Valid( self ) )
		g_notice = pfnNotice;
}

S_API int32 GadgetAPI_IGadgets_Notify( IGadgets *self, int32 nTimes )
{
	if ( !Valid( self ) || !g_notice )
		return 0;
	for ( int32 i = 0; i < nTimes; ++i )
	{
		g_noticeReady.m_eResult = k_EGadgetResultBusy;
		g_noticeReady.m_ulId = 100 + i;
		memcpy( g_noticeText, "notice 0", 9 );
		g_noticeText[ 7 ] = (char)( '0' + i % 10 );
		g_notice( i, &g_noticeReady, g_noticeText );
		memset( &g_noticeReady, 0xdd, sizeof( g_noticeReady ) );
		memset( g_noticeText, 0xdd, sizeof( g_noticeText ) );
	}
	return nTimes;
}

S_API uint32 GadgetAPI_IGadgets_ReadValue( IGadgets *self, const GadgetValue_t *pValue )
{
	if ( !Valid( self ) || !pValue || !pValue->m_val.m_psz )
		return 0xffffffff;
	return (uint32)strlen( pValue->m_val.m_psz );
}

// Like SteamNetworkingConfigValue_t::SetString, these keep the pointer they are given.
S_API void GadgetAPI_GadgetValue_t_SetText( GadgetValue_t *self, const char *pchText )
{
	self->m_eKind = k_EGadgetKindText;
	self->m_val.m_psz = pchText;
}

S_API void GadgetAPI_GadgetValue_t_SetData( GadgetValue_t *self, void *pData ) { self->m_val.m_p = pData; }
