// Avatar metadata method documented by F4b1-/avatar-thrift-store (MIT, 2026).
// See licenses/avatar-thrift-store-MIT.txt. Profile packages are never edited.
#include "AvatarPackage.h"
#include "inc\bearssl.h"
#include <string.h>

static unsigned AvatarBE32(const unsigned char *p)
{return (unsigned(p[0])<<24)|(unsigned(p[1])<<16)|(unsigned(p[2])<<8)|p[3];}
static unsigned long long AvatarBE64(const unsigned char *p)
{return (static_cast<unsigned long long>(AvatarBE32(p))<<32)|AvatarBE32(p+4);}
static void AvatarPut64(unsigned char *p,unsigned long long v)
{for(int i=7;i>=0;--i){p[i]=(unsigned char)v;v>>=8;}}
static std::string AvatarHex(const unsigned char *p,unsigned size)
{static const char h[]="0123456789ABCDEF";std::string out;for(unsigned i=0;i<size;++i){out+=h[p[i]>>4];out+=h[p[i]&15];}return out;}
bool AvatarOnlineXuid(unsigned long long xuid)
{return xuid!=0&&(xuid>>60)!=0xE&&(xuid>>60)!=0xF;}
bool AvatarPatchHeader(std::vector<unsigned char> *header,const std::vector<unsigned long long> &xuids,
                       std::string *relativePath,std::string *contentId,std::string *error)
{
    if(header->size()<0x3F1||xuids.empty()){*error="Avatar unlocking needs a signed-in Live-enabled or activated offline profile";return false;}
    std::vector<unsigned char> h=*header;unsigned size=AvatarBE32(&h[0x340]),end=(size+0xFFF)&~0xFFF;
    if((memcmp(&h[0],"LIVE",4)&&memcmp(&h[0],"PIRS",4)&&memcmp(&h[0],"CON ",4))||AvatarBE32(&h[0x344])!=0x9000||size<0x971A||size>0x100000||end!=h.size())
    {*error="Invalid Avatar package header";return false;}
    br_sha1_context ctx;unsigned char sum[20];br_sha1_init(&ctx);br_sha1_update(&ctx,&h[0x344],end-0x344);br_sha1_out(&ctx,sum);
    if(memcmp(sum,&h[0x32C],20)){*error="Avatar metadata checksum failed";return false;}
    bool asset=false;for(unsigned i=0x3E1;i<0x3F1;++i)asset|=h[i]!=0;
    if(!asset||memcmp(&h[0x3ED],&h[0x360],4)){*error="Avatar asset and package Title IDs differ";return false;}
    for(size_t i=0;i<xuids.size();++i)
    {
        if(!AvatarOnlineXuid(xuids[i])){*error="An offline/local XUID cannot own Avatar items";return false;}
        bool found=false;for(unsigned j=0;j<16;++j)found|=AvatarBE64(&h[0x22C+j*16])==xuids[i];if(found)continue;
        unsigned slot=1;while(slot<16&&AvatarBE64(&h[0x22C+slot*16]))++slot;
        if(slot==16){*error="Avatar license table is full; existing owners were preserved";return false;}
        if(slot>1)memcpy(&h[0x22C+slot*16+8],&h[0x23C+8],8);
        AvatarPut64(&h[0x22C+slot*16],xuids[i]);
    }
    memset(&h[0x360],0xAA,4);memset(&h[0x3ED],0xAA,4);
    br_sha1_init(&ctx);br_sha1_update(&ctx,&h[0x344],end-0x344);br_sha1_out(&ctx,sum);memcpy(&h[0x32C],sum,20);
    *relativePath="AAAAAAAA/00009000/"+AvatarHex(&h[0x3E1],16);*contentId=AvatarHex(sum,20);header->swap(h);return true;
}
