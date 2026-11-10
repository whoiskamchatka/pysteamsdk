// Invented flat functions in the line format of steam_api_flat.h, for the binding generator's
// tests. The tests copy this file into a temporary SDK under the name the extractor expects.
#ifndef GADGETS_FLAT_H
#define GADGETS_FLAT_H

#include "gadgets.h"

typedef uint64 uint64_gadgetid;

// IGadgets
S_API IGadgets *GadgetAPI_Gadgets_v001();
inline IGadgets *GadgetAPI_Gadgets() { return GadgetAPI_Gadgets_v001(); }
S_API int8 GadgetAPI_IGadgets_Echo8( IGadgets* self, int8 nValue );
S_API uint8 GadgetAPI_IGadgets_EchoU8( IGadgets* self, uint8 unValue );
S_API int16 GadgetAPI_IGadgets_Echo16( IGadgets* self, int16 nValue );
S_API uint16 GadgetAPI_IGadgets_EchoU16( IGadgets* self, uint16 unValue );
S_API int32 GadgetAPI_IGadgets_Echo32( IGadgets* self, int32 nValue );
S_API uint32 GadgetAPI_IGadgets_EchoU32( IGadgets* self, uint32 unValue );
S_API int64 GadgetAPI_IGadgets_Echo64( IGadgets* self, int64 nValue );
S_API uint64 GadgetAPI_IGadgets_EchoU64( IGadgets* self, uint64 unValue );
S_API float GadgetAPI_IGadgets_EchoFloat( IGadgets* self, float flValue );
S_API double GadgetAPI_IGadgets_EchoDouble( IGadgets* self, double dValue );
S_API long GadgetAPI_IGadgets_EchoLong( IGadgets* self, long nValue );
S_API bool GadgetAPI_IGadgets_Invert( IGadgets* self, bool bValue );
S_API uint64 GadgetAPI_IGadgets_Mix( IGadgets* self, int8 a, uint16 b, int32 c, uint64 d, float e, double f, bool g, EGadgetColor h );
S_API EGadgetResult GadgetAPI_IGadgets_Classify( IGadgets* self, int32 nValue );
S_API EGadgetColor GadgetAPI_IGadgets_GetColorAll( IGadgets* self );
S_API int32 GadgetAPI_IGadgets_SetMode( IGadgets* self, IGadgets::EMode eMode );
S_API uint32 GadgetAPI_IGadgets_Length( IGadgets* self, const char * pchText );
S_API int32 GadgetAPI_IGadgets_FillName( IGadgets* self, char * pchBuffer, int32 cchBuffer );
S_API const char * GadgetAPI_IGadgets_GetName( IGadgets* self );
S_API const char * GadgetAPI_IGadgets_GetNoName( IGadgets* self );
S_API uint32 GadgetAPI_IGadgets_Checksum( IGadgets* self, const void * pData, uint32 cbData );
S_API uint32 GadgetAPI_IGadgets_Scribble( IGadgets* self, void * pData, uint32 cbData );
S_API bool GadgetAPI_IGadgets_GetCount( IGadgets* self, int32 * pnCount );
S_API bool GadgetAPI_IGadgets_Locate( IGadgets* self, GadgetSpot_t * pSpot );
S_API int64 GadgetAPI_IGadgets_Distance( IGadgets* self, const GadgetSpot_t & from, const GadgetSpot_t & to );
S_API bool GadgetAPI_IGadgets_Explain( IGadgets* self, GadgetErrMsg & errMsg );
S_API GadgetSpot_t GadgetAPI_IGadgets_GetSpot( IGadgets* self );
S_API int64 GadgetAPI_IGadgets_SpotSum( IGadgets* self, GadgetSpot_t spot );
S_API uint32 GadgetAPI_IGadgets_Area( IGadgets* self, GadgetSize_t size );
S_API uint64 GadgetAPI_IGadgets_PairSum( IGadgets* self, GadgetPair_t pair );
S_API void GadgetAPI_IGadgets_SetHook( IGadgets* self, GadgetHook_t pfnHook );
S_API void GadgetAPI_IGadgets_Listen( IGadgets* self, IGadgetListener * pListener );
S_API bool GadgetAPI_IGadgets_ReadTicket( IGadgets* self, GadgetTicket * pTicket );
S_API bool GadgetAPI_IGadgets_Inspect( IGadgets* self, GadgetMystery_t * pMystery );
S_API IGadgetBox * GadgetAPI_IGadgets_OpenBox( IGadgets* self );
S_API GadgetCall_t GadgetAPI_IGadgets_Fetch( IGadgets* self, uint64_gadgetid gadgetID );
S_API uint64_gadgetid GadgetAPI_IGadgets_GetOwner( IGadgets* self );
S_API bool GadgetAPI_IGadgets_GetOwnerInto( IGadgets* self, CGadgetID * pOwner );
S_API bool GadgetAPI_IGadgets_Poll( IGadgets* self, GadgetReady_t * pReady );
S_API bool GadgetAPI_IGadgets_Join( IGadgets* self, GadgetJoined_t * pJoined );
S_API bool GadgetAPI_IGadgets_Describe( IGadgets* self, GadgetValue_t * pValue );
S_API bool GadgetAPI_IGadgets_GetAddress( IGadgets* self, GadgetAddress_t * pAddress );
S_API int32 GadgetAPI_IGadgets_CountNames( IGadgets* self, const char ** ppchNames, int32 nNames );
S_API void GadgetAPI_IGadgets_SetNotice( IGadgets* self, GadgetNotice_t pfnNotice );
S_API int32 GadgetAPI_IGadgets_Notify( IGadgets* self, int32 nTimes );
S_API uint32 GadgetAPI_IGadgets_ReadValue( IGadgets* self, const GadgetValue_t * pValue );

// IGadgetBox
S_API IGadgetBox *GadgetAPI_GadgetBox_v001();
S_API IGadgetBox *GadgetAPI_GameServerGadgetBox_v001();
S_API uint32 GadgetAPI_IGadgetBox_Count( IGadgetBox* self );

// IGadgetListener
S_API void GadgetAPI_IGadgetListener_OnDone( IGadgetListener* self, int32 code );

// GadgetAddress_t
S_API void GadgetAPI_GadgetAddress_t_Clear( GadgetAddress_t* self );
S_API bool GadgetAPI_GadgetAddress_t_IsLoopback( GadgetAddress_t* self );

// GadgetValue_t
S_API void GadgetAPI_GadgetValue_t_SetText( GadgetValue_t* self, const char * pchText );
S_API void GadgetAPI_GadgetValue_t_SetData( GadgetValue_t* self, void * pData );
#endif
