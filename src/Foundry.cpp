// Omni360 GoD Foundry integration, 2026. AGPL-3.0; see LICENSE.
#include "Foundry.h"
#include "FoundryProtocol.h"
#include "ContentWriteLock.h"
#include "GameListUI.h"
#include "Keyboard.h"
#include "downloadFile.h"
#include "GodConvert.h"
#include "InflateSource.h"
#include "ReadAhead.h"
#include "StfsParser.h"
#include "OutputConsole.h"
#include "cJSON.h"
#include "inc\\bearssl.h"
#include "../zlib/zlib.h"
#include <xtl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <algorithm>
#include <map>

static const char *API="/api/console/v1";
static const char *DEVICE="game:\\FoundryDevice.json";
static const char *JOB="game:\\FoundryJob.json";
static const char *TXN="game:\\FoundryTxn.json";
static const unsigned long long PIECE=32ULL*1024*1024;
static const int JOB_ID=3000000;

struct Device
{
    std::string origin, token, id, mode;
    unsigned long long sequence;
    Device():origin("https://iso2god.mediahub.stream"),mode("server"),sequence(0){}
};
struct Work
{
    std::string request, name, games, content;
};
struct Move
{
    std::string root, from, to;
};
struct InstalledIdentity { std::string title, media, base; };
static Device g_device;
static Work g_work;
static CRITICAL_SECTION g_lock;
static HANDLE g_thread=NULL, g_wake=NULL;
static volatile LONG g_stop=0, g_cancel=0, g_uiCancel=0;
static bool g_started=false,g_pending=false,g_used=false,g_delivered=false;
static QueueJobSnapshot g_snap;

static std::string Text(const cJSON *o,const char *k)
{
    const cJSON *v=cJSON_GetObjectItemCaseSensitive(o,k);
    return cJSON_IsString(v)&&v->valuestring?v->valuestring:"";
}
static std::string Decimal(unsigned long long n)
{
    char s[32]; _snprintf(s,sizeof(s),"%I64u",n); s[sizeof(s)-1]=0; return s;
}
static bool Exists(const std::string &p) { return GetFileAttributesA(p.c_str())!=INVALID_FILE_ATTRIBUTES; }
static std::string Native(std::string p) { std::replace(p.begin(),p.end(),'/','\\'); return p; }
static bool Dirs(const std::string &p)
{
    for(size_t i=p.find('\\')+1;i<=p.size();++i) if(i==p.size()||p[i]=='\\')
    {
        std::string sub=p.substr(0,i);
        if(!sub.empty() && !Exists(sub) && !CreateDirectoryA(sub.c_str(),NULL)) return false;
    }
    return true;
}
static bool Parents(const std::string &p) { return Dirs(p.substr(0,p.find_last_of('\\'))); }
static void RemoveTree(const std::string &path)
{
    WIN32_FIND_DATAA f; HANDLE h=FindFirstFileA((path+"\\*").c_str(),&f);
    if(h!=INVALID_HANDLE_VALUE)
    {
        do {
            if(!strcmp(f.cFileName,".")||!strcmp(f.cFileName,"..")) continue;
            std::string p=path+"\\"+f.cFileName;
            if(f.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY) RemoveTree(p); else DeleteFileA(p.c_str());
        } while(FindNextFileA(h,&f));
        FindClose(h);
    }
    RemoveDirectoryA(path.c_str());
}
static bool WriteJson(const char *path,cJSON *value)
{
    char *data=cJSON_PrintUnformatted(value); if(!data) return false;
    std::string tmp=std::string(path)+".tmp",old=std::string(path)+".old";
    size_t n=strlen(data);DWORD written=0;
    HANDLE f=CreateFileA(tmp.c_str(),GENERIC_WRITE,0,NULL,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,NULL);
    bool ok=f!=INVALID_HANDLE_VALUE && WriteFile(f,data,(DWORD)n,&written,NULL) && written==n && FlushFileBuffers(f);
    if(f!=INVALID_HANDLE_VALUE)CloseHandle(f);
    cJSON_free(data);
    if(!ok) return false;
    DeleteFileA(old.c_str());
    if(Exists(path) && !MoveFileA(path,old.c_str())) return false;
    if(!MoveFileA(tmp.c_str(),path)) { MoveFileA(old.c_str(),path); return false; }
    DeleteFileA(old.c_str()); return true;
}
static cJSON *ReadJson(const char *path,unsigned limit=1024*1024)
{
    // Recover the prior complete generation if interrupted between renames.
    std::string old=std::string(path)+".old";
    if(!Exists(path)&&Exists(old)) MoveFileA(old.c_str(),path);
    FILE *f=fopen(path,"rb"); if(!f) return NULL;
    fseek(f,0,SEEK_END); long n=ftell(f); fseek(f,0,SEEK_SET);
    if(n<=0 || (unsigned long)n>limit) { fclose(f); return NULL; }
    std::vector<char> data(n+1); bool ok=fread(&data[0],1,n,f)==(size_t)n; fclose(f); data[n]=0;
    return ok?cJSON_Parse(&data[0]):NULL;
}
static bool SaveDevice()
{
    cJSON *o=cJSON_CreateObject();
    cJSON_AddStringToObject(o,"origin",g_device.origin.c_str()); cJSON_AddStringToObject(o,"token",g_device.token.c_str());
    cJSON_AddStringToObject(o,"id",g_device.id.c_str()); cJSON_AddStringToObject(o,"mode",g_device.mode.c_str());
    cJSON_AddStringToObject(o,"sequence",Decimal(g_device.sequence).c_str());
    bool ok=WriteJson(DEVICE,o); cJSON_Delete(o); return ok;
}
static void LoadDevice()
{
    cJSON *o=ReadJson(DEVICE,4096); if(!o) return;
    std::string origin=Text(o,"origin"),token=Text(o,"token"),id=Text(o,"id"),mode=Text(o,"mode");
    if(FoundryOrigin(origin)) g_device.origin=origin;
    if(token.size()>=40 && token.size()<100 && token.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")==std::string::npos && FoundryHex(id,32))
    { g_device.token=token;g_device.id=id; }
    if(mode=="console"||mode=="server") g_device.mode=mode;
    FoundryDecimal(Text(o,"sequence").c_str(),&g_device.sequence); cJSON_Delete(o);
}
static Device DeviceCopy() { EnterCriticalSection(&g_lock);Device d=g_device;LeaveCriticalSection(&g_lock);return d; }
static void Phase(const char *name,float fraction=-1.0f,unsigned long long done=0,unsigned long long total=0)
{
    EnterCriticalSection(&g_lock);
    _snprintf(g_snap.phase,sizeof(g_snap.phase),"%s",name); g_snap.phase[sizeof(g_snap.phase)-1]=0;
    g_snap.fraction=fraction;g_snap.bytesDone=done;g_snap.bytesTotal=total;
    LeaveCriticalSection(&g_lock);
}
static bool Cancelled() { return InterlockedCompareExchange(&g_cancel,0,0)||InterlockedCompareExchange(&g_stop,0,0); }
static bool DownloadProgress(unsigned long long done,unsigned long long total,unsigned long long speed,unsigned long long left)
{
    EnterCriticalSection(&g_lock);g_snap.bytesDone=done;g_snap.bytesTotal=total;g_snap.bytesPerSec=speed;g_snap.secondsLeft=left;
    g_snap.fraction=total?(float)((double)done/total):-1.0f;LeaveCriticalSection(&g_lock);return !Cancelled();
}
static bool UiProgress(unsigned long long,unsigned long long,unsigned long long,unsigned long long)
{ return !InterlockedCompareExchange(&g_uiCancel,0,0); }

static bool Call(const std::string &path,const std::string &body,bool auth,cJSON **reply,std::string *error,bool ui=false)
{
    Device d=DeviceCopy();
    if(!FoundryOrigin(d.origin)||path.compare(0,strlen(API),API)!=0||path.find_first_of("\r\n")!=std::string::npos)
    { *error="Invalid Foundry URL";return false; }
    std::string headers=auth?"Authorization: Bearer "+d.token+"\r\n":"";
    HttpResponsePolicy policy;policy.json=true;
    std::vector<char> buffer(1024*1024+1);unsigned long long len=buffer.size()-1;
    int status=httpRequestHTTPS(d.origin+path,body.empty()?HTTP_GET:HTTP_POST,body.empty()?NULL:body.c_str(),headers.c_str(),"",
        &buffer[0],&len,false,NULL,0,dprintf,0,ui?UiProgress:NULL,false,&policy);
    if(len>=buffer.size()) len=buffer.size()-1;buffer[(size_t)len]=0;
    cJSON *result=cJSON_Parse(&buffer[0]);
    if(status<200||status>=300||!cJSON_IsObject(result))
    {
        *error=Text(result,"error");
        if(error->empty()) *error=status==401?"Pair this console again":"Could not reach Foundry over verified HTTPS";
        cJSON_Delete(result);return false;
    }
    *reply=result;return true;
}
struct UiTask
{
    std::string path,body,error;bool auth,ok;cJSON *reply;
    UiTask():auth(true),ok(false),reply(NULL){}
};
static DWORD WINAPI UiTaskEntry(LPVOID p)
{ UiTask *t=(UiTask*)p;t->ok=Call(t->path,t->body,t->auth,&t->reply,&t->error,true);return 0; }
static bool UiCall(const std::string &path,const std::string &body,bool auth,cJSON **reply)
{
    UiTask task;task.path=path;task.body=body;task.auth=auth;InterlockedExchange(&g_uiCancel,0);
    HANDLE thread=CreateThread(NULL,256*1024,UiTaskEntry,&task,0,NULL);
    if(!thread) { ShowMessageUI("Foundry","Could not start the network request",NULL);return false; }
    while(WaitForSingleObject(thread,0)!=WAIT_OBJECT_0)
    {
        RenderProgressFrame("Foundry","Contacting your server","B cancels this request",-1,"Connecting");
        if(PollUiInput().pressed&XINPUT_GAMEPAD_B) InterlockedExchange(&g_uiCancel,1);
        Sleep(16);
    }
    CloseHandle(thread);ResyncUiInput();
    if(!task.ok) { ShowMessageUI("Foundry",task.error.c_str(),NULL);return false; }
    *reply=task.reply;return true;
}
static std::string Encode(const std::string &s)
{
    static const char hex[]="0123456789ABCDEF";std::string out;
    for(size_t i=0;i<s.size();++i)
    {
        unsigned char c=s[i];
        if((c>='a'&&c<='z')||(c>='A'&&c<='Z')||(c>='0'&&c<='9')||c=='-'||c=='_') out+=(char)c;
        else {out+='%';out+=hex[c>>4];out+=hex[c&15];}
    }
    return out;
}
static std::string Root(const Work &w,const std::string &kind)
{
    if(kind=="games") return w.games;
    if(kind=="content") return w.content;
    if(kind=="cache") return "Hdd1:\\Cache";
    return "";
}
static std::string Stage(const std::string &root,const std::string &aid) { return root+"\\_Foundry\\"+aid; }
static bool FreeSpace(const std::string &root,unsigned long long needed)
{
    ULARGE_INTEGER avail,total,freeBytes;
    return GetDiskFreeSpaceExA((root.substr(0,root.find('\\'))+"\\").c_str(),&avail,&total,&freeBytes) && avail.QuadPart>=needed+1024ULL*1024*1024;
}
static bool HashFile(const std::string &path,std::string *hash,unsigned long long expected=0)
{
    FILE *f=fopen(path.c_str(),"rb");if(!f)return false;
    br_sha256_context sha;br_sha256_init(&sha);std::vector<unsigned char> block(256*1024);unsigned long long n=0;
    size_t got;while((got=fread(&block[0],1,block.size(),f))>0){br_sha256_update(&sha,&block[0],got);n+=got;}
    bool ok=!ferror(f)&&(!expected||n==expected);fclose(f);
    unsigned char bytes[32];br_sha256_out(&sha,bytes);char text[65];
    for(int i=0;i<32;++i)sprintf(text+i*2,"%02x",bytes[i]);text[64]=0;*hash=text;return ok;
}
static unsigned long long Length(const std::string &path)
{
    WIN32_FILE_ATTRIBUTE_DATA d;if(!GetFileAttributesExA(path.c_str(),GetFileExInfoStandard,&d))return 0;
    return ((unsigned long long)d.nFileSizeHigh<<32)|d.nFileSizeLow;
}
static bool Fetch(const std::string &path,const std::string &destination,unsigned long long bytes,const std::string &sha,std::string *error)
{
    std::string hash;
    if(HashFile(destination,&hash,bytes)&&hash==sha)return true;
    if(!Parents(destination)){*error="Could not create the staging folder";return false;}
    size_t filesAt=destination.find("\\files\\");
    if(filesAt==std::string::npos){*error="Invalid staging destination";return false;}
    std::string partial=destination.substr(0,filesAt)+"\\parts\\"+path.substr(path.find_last_of('/')+1)+".part";
    if(!Parents(partial)){*error="Could not create partial-file storage";return false;}
    unsigned long long offset=Length(partial);
    if(offset>bytes){DeleteFileA(partial.c_str());offset=0;}
    if(offset<bytes)
    {
        Device d=DeviceCopy();std::string headers="Authorization: Bearer "+d.token+"\r\n";
        std::string etag="\""+sha+"\"",range;
        HttpResponsePolicy policy;policy.status=offset?206:200;policy.etag=etag.c_str();
        if(offset)
        {
            headers+="Range: bytes="+Decimal(offset)+"-\r\nIf-Range: "+etag+"\r\n";
            range="bytes "+Decimal(offset)+"-"+Decimal(bytes-1)+"/"+Decimal(bytes);policy.contentRange=range.c_str();
        }
        int status=httpRequestHTTPS(d.origin+path,HTTP_GET,NULL,headers.c_str(),partial,NULL,NULL,true,NULL,0,dprintf,bytes-offset,DownloadProgress,offset!=0,&policy);
        if(status!=policy.status){*error=Cancelled()?"Cancelled; downloaded parts are kept":"Download interrupted; retry to resume";return false;}
    }
    if(Length(partial)<bytes){*error="Download interrupted; partial file kept for resume";return false;}
    if(!HashFile(partial,&hash,bytes)||hash!=sha){DeleteFileA(partial.c_str());*error="Downloaded file failed its SHA-256 check";return false;}
    DeleteFileA(destination.c_str());if(!MoveFileA(partial.c_str(),destination.c_str())){*error="Could not save the checked file";return false;}return true;
}
static unsigned long BE32(const unsigned char *p) {return ((unsigned long)p[0]<<24)|((unsigned long)p[1]<<16)|((unsigned long)p[2]<<8)|p[3];}
static std::string Hex32(unsigned long v){char s[9];_snprintf(s,9,"%08lX",v);return s;}
class InstalledGodSource : public GodSource
{
    std::string header;unsigned long long length;
public:
    InstalledGodSource(const std::string &p):header(p),length(0)
    {
        FILE *f=fopen(p.c_str(),"rb");unsigned char h[0x395];
        if(f){if(fread(h,1,sizeof(h),f)==sizeof(h))length=((unsigned long long)h[0x392]*65536+h[0x393]*256+h[0x394])*4096;fclose(f);}
    }
    unsigned long long Size(){return length;}
    bool ReadAt(unsigned long long offset,void *output,unsigned long size)
    {
        if(offset>length||size>length-offset)return false;unsigned char *out=(unsigned char*)output;
        while(size)
        {
            unsigned long long block=offset/4096;unsigned long within=(unsigned long)(offset%4096),part=(unsigned long)(block/0xA1C4);
            unsigned long local=(unsigned long)(block%0xA1C4),physical=2+(local/204)*205+local%204;
            char name[32];_snprintf(name,sizeof(name),"\\Data%04lu",part);std::string path=header+".data"+name;
            FILE *f=fopen(path.c_str(),"rb");if(!f)return false;unsigned long n=std::min(size,4096-within);
            bool ok=fseek(f,(long)(physical*4096+within),SEEK_SET)==0&&fread(out,1,n,f)==n;fclose(f);if(!ok)return false;
            offset+=n;size-=n;out+=n;
        }
        return true;
    }
};
static bool MatchingInstalledGame(const Work &w,const FoundryManifest &m,const FoundryComponent &update)
{
    std::vector<std::string> candidates;
    for(size_t i=0;i<m.components.size();++i)
    {
        const FoundryComponent &c=m.components[i];
        if(c.kind=="god"&&c.titleId==update.titleId&&c.mediaId==update.mediaId)
            for(size_t j=0;j<c.files.size();++j)if(c.files[j].role=="header")candidates.push_back(Stage(w.games,m.id)+"\\files\\"+Native(c.files[j].path));
    }
    WIN32_FIND_DATAA fd;std::string dir=w.games+"\\"+update.titleId+"\\00007000";
    HANDLE h=FindFirstFileA((dir+"\\*").c_str(),&fd);
    if(h!=INVALID_HANDLE_VALUE){do{if(!(fd.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY))candidates.push_back(dir+"\\"+fd.cFileName);}while(FindNextFileA(h,&fd));FindClose(h);}
    for(size_t i=0;i<candidates.size();++i)
    {
        InstalledGodSource source(candidates[i]);GodImageInfo info;
        if(GodInspect(&source,&info)==GOD_OK&&Hex32(info.title.titleId)==update.titleId&&Hex32(info.title.mediaId)==update.mediaId&&Hex32(info.title.baseVersion)==update.baseVersion)return true;
    }
    return false;
}
static bool CheckPackage(const std::string &path,const FoundryComponent &c,std::string *error)
{
    FILE *f=fopen(path.c_str(),"rb");if(!f){*error="Package is missing";return false;}
    std::vector<unsigned char> header(0x344);bool ok=fread(&header[0],1,header.size(),f)==header.size();
    if(!ok|| (memcmp(&header[0],"LIVE",4)&&memcmp(&header[0],"PIRS",4)&&memcmp(&header[0],"CON ",4))){fclose(f);*error="Invalid content header";return false;}
    unsigned long size=BE32(&header[0x340]),end=(size+0xfff)&~0xfff;
    if(size<0x971a||end>0x100000||end>Length(path)){fclose(f);*error="Invalid metadata length";return false;}
    header.resize(end);fseek(f,0,SEEK_SET);ok=fread(&header[0],1,end,f)==end;fclose(f);
    br_sha1_context sha;br_sha1_init(&sha);br_sha1_update(&sha,&header[0x344],end-0x344);unsigned char sum[20];br_sha1_out(&sha,sum);
    ok=ok&&memcmp(sum,&header[0x32c],20)==0&&Hex32(BE32(&header[0x360]))==c.titleId&&Hex32(BE32(&header[0x344]))==c.contentType;
    if(!c.contentId.empty())
    {
        static const char hex[]="0123456789ABCDEF";std::string content;
        for(unsigned i=0x32c;i<0x340;++i){content+=hex[header[i]>>4];content+=hex[header[i]&15];}
        ok=ok&&content==c.contentId;
    }
    if(c.kind=="god")
    {
        bool licensed=false;for(unsigned i=0x22c;i<0x32c;++i)if(header[i])licensed=true;
        ok=ok&&licensed;
    }
    if(c.kind=="god"||c.kind=="title_update")ok=ok&&Hex32(BE32(&header[0x354]))==c.mediaId;
    if(c.kind=="title_update")ok=ok&&Hex32(BE32(&header[0x35c]))==c.baseVersion;
    if(!ok){*error="Package metadata does not match the installation";return false;}
    return true;
}
static bool SaveJournal(const Work &w,const std::string &aid,const std::vector<Move> &moves,bool committed)
{
    cJSON *o=cJSON_CreateObject(),*a=cJSON_AddArrayToObject(o,"moves");
    cJSON_AddStringToObject(o,"artifact",aid.c_str());cJSON_AddStringToObject(o,"games",w.games.c_str());cJSON_AddStringToObject(o,"content",w.content.c_str());
    cJSON_AddBoolToObject(o,"committed",committed);
    for(size_t i=0;i<moves.size();++i){cJSON *m=cJSON_CreateObject();cJSON_AddStringToObject(m,"root",moves[i].root.c_str());cJSON_AddStringToObject(m,"from",moves[i].from.c_str());cJSON_AddStringToObject(m,"to",moves[i].to.c_str());cJSON_AddItemToArray(a,m);}
    bool ok=WriteJson(TXN,o);cJSON_Delete(o);return ok;
}
static bool RootValid(const std::string &s)
{return s.size()>5&&s.size()<170&&s.find(":\\")!=std::string::npos&&s.find("..") == std::string::npos&&s[s.size()-1]!='\\';}
static bool Recover(std::string *error)
{
    ContentWriteLock lock;if(!lock.Ok()){*error="Could not lock content storage";return false;}
    cJSON *o=ReadJson(TXN);if(!o){if(Exists(TXN)){*error="Recovery journal is invalid; preserve it and inspect storage before retrying";return false;}return true;}
    Work w;w.games=Text(o,"games");w.content=Text(o,"content");std::string aid=Text(o,"artifact");
    const cJSON *a=cJSON_GetObjectItemCaseSensitive(o,"moves");bool ok=FoundryHex(aid,32)&&RootValid(w.games)&&RootValid(w.content)&&cJSON_IsArray(a)&&cJSON_GetArraySize(a)<1000;
    bool committed=cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(o,"committed"));
    for(int i=cJSON_GetArraySize(a)-1;ok&&!committed&&i>=0;--i)
    {
        const cJSON *m=cJSON_GetArrayItem(a,i);std::string root=Root(w,Text(m,"root")),from=Text(m,"from"),to=Text(m,"to");
        ok=!root.empty()&&FoundrySafePath(from)&&FoundrySafePath(to)
            && (from.compare(0,9,"_Foundry/")==0||FoundryHex(from.substr(0,8),8))
            && (to.compare(0,9,"_Foundry/")==0||FoundryHex(to.substr(0,8),8));
        if(!ok)break;
        from=root+"\\"+Native(from);to=root+"\\"+Native(to);
        if(!Exists(from)&&Exists(to)){ok=Parents(from)&&MoveFileA(to.c_str(),from.c_str());}
        else if(!Exists(from)&&!Exists(to))ok=false;
    }
    if(ok)
    {
        // Committed generations may discard rollback, but keep source pieces
        // until the worker has persisted its installed result.
        if(committed){RemoveTree(Stage(w.games,aid)+"\\rollback");RemoveTree(Stage(w.content,aid)+"\\rollback");RemoveTree(Stage(Root(w,"cache"),aid)+"\\rollback");}
        DeleteFileA(TXN);DeleteFileA((std::string(TXN)+".old").c_str());
    }
    cJSON_Delete(o);if(!ok)*error="Interrupted installation needs its original storage attached; no new files were installed";return ok;
}
static void AddMove(std::vector<Move> *moves,const std::string &root,const std::string &from,const std::string &to)
{Move m;m.root=root;m.from=from;m.to=to;moves->push_back(m);}
static bool Commit(const Work &w,const FoundryManifest &m,std::string *error)
{
    ContentWriteLock lock;if(!lock.Ok()){*error="Could not lock content storage";return false;}
    std::vector<Move> moves;
    for(size_t i=0;i<m.components.size();++i)
    {
        const FoundryComponent &c=m.components[i];std::string root=c.kind=="god"?"games":c.files[0].root=="cache"?"cache":"content";
        std::string staging="_Foundry/"+m.id;
        if(c.kind=="god")
        {
            std::string header;
            for(size_t j=0;j<c.files.size();++j)if(c.files[j].role=="header")header=c.files[j].path;
            // Hide and back up every existing package for this exact disc;
            // preserve other discs and add-ons, even if basenames differ.
            std::string dir=Root(w,root)+"\\"+c.titleId+"\\00007000";WIN32_FIND_DATAA fd;
            HANDLE h=FindFirstFileA((dir+"\\*").c_str(),&fd);
            if(h!=INVALID_HANDLE_VALUE)
            {
                do {
                    if(fd.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY)continue;
                    std::string p=dir+"\\"+fd.cFileName;FILE *f=fopen(p.c_str(),"rb");unsigned char head[0x364];
                    bool match=f&&fread(head,1,sizeof(head),f)==sizeof(head)&&!memcmp(head,"LIVE",4)&&BE32(head+0x344)==0x7000&&Hex32(BE32(head+0x360))==c.titleId&&Hex32(BE32(head+0x354))==c.mediaId;
                    if(f)fclose(f);if(!match)continue;
                    std::string rel=c.titleId+"/00007000/"+fd.cFileName;
                    AddMove(&moves,root,rel,staging+"/rollback/"+rel);
                    if(Exists(p+".data"))AddMove(&moves,root,rel+".data",staging+"/rollback/"+rel+".data");
                }while(FindNextFileA(h,&fd));FindClose(h);
            }
            bool backedData=false;
            for(size_t k=0;k<moves.size();++k)if(moves[k].root==root&&moves[k].from==header+".data")backedData=true;
            if(Exists(Root(w,root)+"\\"+Native(header)+".data") && !backedData) {*error="An orphaned GoD data folder needs review before replacement";return false;}
            AddMove(&moves,root,staging+"/files/"+header+".data",header+".data");
            AddMove(&moves,root,staging+"/files/"+header,header);
        }
        else
        {
            const std::string &rel=c.files[0].path;
            if(Exists(Root(w,root)+"\\"+Native(rel)))AddMove(&moves,root,rel,staging+"/rollback/"+rel);
            AddMove(&moves,root,staging+"/files/"+rel,rel);
        }
    }
    if(!SaveJournal(w,m.id,moves,false)){*error="Could not save the installation recovery journal";return false;}
    for(size_t i=0;i<moves.size();++i)
    {
        const Move &move=moves[i];std::string root=Root(w,move.root),from=root+"\\"+Native(move.from),to=root+"\\"+Native(move.to);
        if(!Parents(to)||Exists(to)||!MoveFileA(from.c_str(),to.c_str())){*error="Install commit failed; restoring previous packages";std::string recover;Recover(&recover);return false;}
    }
    if(!SaveJournal(w,m.id,moves,true)){*error="Could not finish the installation journal";return false;}
    return Recover(error);
}
static bool Prepared(const Work &w,const FoundryManifest &m,std::string *error)
{
    std::map<std::string,unsigned long long> required;
    for(size_t i=0;i<m.components.size();++i)
    {
        const FoundryComponent &c=m.components[i];std::string root=Root(w,c.kind=="god"?"games":c.files[0].root=="cache"?"cache":"content");
        if(c.files[0].root=="cache"&&!Exists("Hdd1:\\")){*error="This legacy title update requires an internal Hdd1 Cache destination";return false;}
        for(size_t j=0;j<c.files.size();++j)
        {
            const FoundryFile &f=c.files[j];unsigned long long staged=Length(Stage(root,m.id)+"\\files\\"+Native(f.path));
            unsigned long long partial=Length(Stage(root,m.id)+"\\parts\\"+f.id+".part");
            unsigned long long available=staged==f.bytes?staged:partial<=f.bytes?partial:0;
            required[root]+=f.bytes-available;
        }
    }
    for(std::map<std::string,unsigned long long>::const_iterator it=required.begin();it!=required.end();++it)
        if(!FreeSpace(it->first,it->second)){*error="Not enough free space on the selected destination for staging and recovery";return false;}
    for(size_t i=0;i<m.components.size();++i)
    {
        const FoundryComponent &c=m.components[i];std::string root=Root(w,c.kind=="god"?"games":c.files[0].root=="cache"?"cache":"content");
        if(c.kind=="title_update"&&!MatchingInstalledGame(w,m,c)){*error="Title update does not match the installed game's actual Media ID and base version";return false;}
        for(size_t j=0;j<c.files.size();++j)
        {
            if(Cancelled()){*error="Cancelled; checked files are kept for resume";return false;}
            const FoundryFile &f=c.files[j];Phase("Downloading checked package");
            if(!Fetch(std::string(API)+"/artifacts/"+m.id+"/files/"+f.id,Stage(root,m.id)+"\\files\\"+Native(f.path),f.bytes,f.sha256,error))return false;
        }
        const FoundryFile *package=&c.files[0];for(size_t j=0;j<c.files.size();++j)if(c.files[j].role=="header")package=&c.files[j];
        Phase("Checking package metadata");if(!CheckPackage(Stage(root,m.id)+"\\files\\"+Native(package->path),c,error))return false;
    }
    if(Cancelled()){*error="Cancelled before installation; checked files are kept";return false;}
    Phase("Installing; recovery journal saved");return Commit(w,m,error);
}
class StoredImage : public GodSource
{
    ZipPieces *pieces;unsigned long long start,length;
public:
    StoredImage(ZipPieces *p,unsigned long long s,unsigned long long n):pieces(p),start(s),length(n){}
    bool ReadAt(unsigned long long o,void *b,unsigned long n){return o<=length&&n<=length-o&&pieces->Read(start+o,b,n);}
    unsigned long long Size(){return length;}
};
static bool ConvertProgress(unsigned long long done,unsigned long long total,void *)
{Phase("Converting on Xbox",total?(float)((double)done/total):-1,done,total);return !Cancelled();}
static bool ConsoleConvert(const Work &w,const FoundryManifest &m,std::string *error)
{
    const FoundrySource &s=m.source;std::string dir=Stage(w.games,m.id)+"\\source";
    if(Exists(dir+"\\corrupt.flag"))RemoveTree(dir);
    if(!Dirs(dir)||!FreeSpace(w.games,s.bytes+s.unpacked+s.unpacked/20)){*error="Not enough space for source pieces and the converted game";return false;}
    ZipPieces pieces(dir.c_str(),PIECE);Device device=DeviceCopy();
    br_sha256_context sha;br_sha256_init(&sha);std::vector<unsigned char> buffer(256*1024);
    for(unsigned long long at=0;at<s.bytes;at+=PIECE)
    {
        if(Cancelled()){*error="Cancelled; source pieces are kept";return false;}
        unsigned long long size=std::min(PIECE,s.bytes-at);char path[512];pieces.PiecePath((unsigned long)(at/PIECE),path,sizeof(path));
        if(Length(path)!=size)
        {
            Phase("Downloading ISO archive in pieces");
            std::string partial=std::string(path)+".partial",etag="\""+s.sha256+"\"";
            std::string headers="Authorization: Bearer "+device.token+"\r\nRange: bytes="+Decimal(at)+"-"+Decimal(at+size-1)+"\r\nIf-Range: "+etag+"\r\n";
            std::string range="bytes "+Decimal(at)+"-"+Decimal(at+size-1)+"/"+Decimal(s.bytes);
            HttpResponsePolicy policy;policy.status=206;policy.etag=etag.c_str();policy.contentRange=range.c_str();
            int status=httpRequestHTTPS(device.origin+API+"/source-artifacts/"+m.id+"/file",HTTP_GET,NULL,headers.c_str(),partial,NULL,NULL,true,NULL,0,dprintf,size,DownloadProgress,false,&policy);
            if(status!=206||Length(partial)!=size){*error="Source download interrupted; retry to resume";return false;}
            DeleteFileA(path);if(!MoveFileA(partial.c_str(),path)){*error="Could not save source piece";return false;}
        }
        for(unsigned long long n=0;n<size;n+=buffer.size())
        {unsigned long count=(unsigned long)std::min((unsigned long long)buffer.size(),size-n);if(!pieces.Read(at+n,&buffer[0],count)){*error="Source piece read failed";return false;}br_sha256_update(&sha,&buffer[0],count);}
    }
    unsigned char sum[32];char hex[65];br_sha256_out(&sha,sum);for(int i=0;i<32;++i)sprintf(hex+i*2,"%02x",sum[i]);hex[64]=0;
    if(s.sha256!=hex){FILE *flag=fopen((dir+"\\corrupt.flag").c_str(),"wb");if(flag)fclose(flag);*error="Source checksum failed; retry will fetch clean source pieces";return false;}
    Phase("Checking ISO and building conversion index");
    InflateIndexer index(s.unpacked);unsigned long crc=crc32(0L,Z_NULL,0);
    for(unsigned long long at=0;at<s.packed;at+=buffer.size())
    {
        if(Cancelled()){*error="Cancelled; source archive is kept";return false;}
        unsigned long n=(unsigned long)std::min((unsigned long long)buffer.size(),s.packed-at);
        if(!pieces.Read(s.offset+at,&buffer[0],n)){*error="ISO read failed";return false;}
        if(s.method==8){if(!index.Ok()||!index.Feed(&buffer[0],n)){*error="Invalid compressed ISO";return false;}}
        else crc=crc32(crc,&buffer[0],n);
    }
    if((s.method==8&&(!index.Finished()||index.Crc()!=s.crc||index.Out()!=s.unpacked))||(s.method==0&&crc!=s.crc)){*error="ISO CRC or length check failed";return false;}
    int count=0;InflatePoint *points=s.method==8?index.TakePoints(&count):NULL;
    InflateSource inflated(&pieces,s.offset,s.packed,s.unpacked,points,count);StoredImage stored(&pieces,s.offset,s.unpacked);
    GodSource *image=s.method==8?(GodSource*)&inflated:(GodSource*)&stored;GodImageInfo info;
    GodResult result=GodInspect(image,&info);
    if(result!=GOD_OK||info.title.contentType!=GOD_CONTENT_GAMES_ON_DEMAND||(!s.titleId.empty()&&Hex32(info.title.titleId)!=s.titleId))
    {free(points);*error="ISO inspection failed or the disc identity differs";return false;}
    if(!FreeSpace(w.games,info.outputSize)){free(points);*error="Not enough room for the converted game";return false;}
    std::string output=Stage(w.games,m.id)+"\\files";Dirs(output);char package[600];
    {ReadAheadSource ahead(image,1024*1024,8);result=GodConvert(&ahead,info,output.c_str(),s.name.c_str(),NULL,0,ConvertProgress,NULL,package,sizeof(package));}
    free(points);if(result!=GOD_OK){*error=GodResultText(result);return false;}
    FoundryManifest generated;generated.id=m.id;FoundryComponent c;c.kind="god";c.titleId=Hex32(info.title.titleId);c.mediaId=Hex32(info.title.mediaId);c.contentType="00007000";
    std::string relative=std::string(package).substr(output.size()+1);std::replace(relative.begin(),relative.end(),'\\','/');
    FoundryFile header;header.path=relative;header.root="content";header.role="header";c.files.push_back(header);generated.components.push_back(c);
    if(!CheckPackage(package,c,error))return false;
    Phase("Installing converted game");return Commit(w,generated,error);
}
static bool SaveWork(const Work &w)
{
    cJSON *o=cJSON_CreateObject();cJSON_AddStringToObject(o,"request",w.request.c_str());cJSON_AddStringToObject(o,"name",w.name.c_str());
    cJSON_AddStringToObject(o,"games",w.games.c_str());cJSON_AddStringToObject(o,"content",w.content.c_str());
    bool ok=WriteJson(JOB,o);cJSON_Delete(o);return ok;
}
static void Finish(bool ok,const std::string &error)
{
    EnterCriticalSection(&g_lock);g_snap.state=QUEUE_FINISHED;g_snap.outcome=ok?QUEUE_OUTCOME_INSTALLED:Cancelled()?QUEUE_OUTCOME_CANCELLED:QUEUE_OUTCOME_FAILED;
    g_snap.fraction=ok?1:-1;g_snap.notify=true;g_delivered=false;
    _snprintf(g_snap.resultText,sizeof(g_snap.resultText),"%s",ok?"Installed":Cancelled()?"Paused":"Installation failed");
    _snprintf(g_snap.resultDetail,sizeof(g_snap.resultDetail),"%s",ok?"Ready in the library. Enable selected title updates in Aurora.":error.c_str());
    g_snap.resultText[sizeof(g_snap.resultText)-1]=0;g_snap.resultDetail[sizeof(g_snap.resultDetail)-1]=0;LeaveCriticalSection(&g_lock);
}
static void InitSnap(const Work &w)
{
    memset(&g_snap,0,sizeof(g_snap));g_snap.id=JOB_ID;g_snap.kind=QUEUE_JOB_GAME_INSTALL;g_snap.state=QUEUE_WAITING;g_snap.fraction=-1;
    _snprintf(g_snap.gameName,sizeof(g_snap.gameName),"%s",w.name.c_str());_snprintf(g_snap.title,sizeof(g_snap.title),"Foundry: %s",w.name.c_str());
    g_snap.gameName[sizeof(g_snap.gameName)-1]=0;g_snap.title[sizeof(g_snap.title)-1]=0;g_used=true;g_delivered=false;
}
static DWORD WINAPI Worker(LPVOID)
{
    while(!InterlockedCompareExchange(&g_stop,0,0))
    {
        WaitForSingleObject(g_wake,INFINITE);if(g_stop)break;
        EnterCriticalSection(&g_lock);bool pending=g_pending;g_pending=false;Work w=g_work;g_snap.state=QUEUE_ACTIVE;LeaveCriticalSection(&g_lock);
        if(!pending)continue;std::string error;bool ok=Recover(&error);std::string aid;
        while(ok&&!Cancelled())
        {
            Phase("Waiting for server preparation");cJSON *reply=NULL;
            if(!Call(std::string(API)+"/install-requests/"+w.request,"",true,&reply,&error)){ok=false;break;}
            std::string state=Text(reply,"state"),message=Text(reply,"message");aid=Text(reply,"artifact_id");cJSON_Delete(reply);
            if(state=="ready")break;
            if(state=="failed"||state=="expired"||state=="cancelled"){error=message;ok=false;break;}
            Phase(message.c_str());for(int i=0;i<30&&!Cancelled();++i)Sleep(100);
        }
        if(Cancelled()){error="Paused; choose Resume in Foundry";ok=false;}
        if(ok)
        {
            cJSON *reply=NULL;ok=FoundryHex(aid,32)&&Call(std::string(API)+"/artifacts/"+aid+"/manifest","",true,&reply,&error);
            if(ok)
            {
                char *raw=cJSON_PrintUnformatted(reply);FoundryManifest manifest;
                ok=FoundryParseManifest(raw,&manifest,&error)&&manifest.id==aid;cJSON_free(raw);cJSON_Delete(reply);
                if(ok)ok=manifest.conversion=="server"?Prepared(w,manifest,&error):ConsoleConvert(w,manifest,&error);
            }
        }
        if(ok)
        {
            DeleteFileA(JOB);DeleteFileA((std::string(JOB)+".old").c_str());
            RemoveTree(Stage(w.games,aid));if(w.content!=w.games)RemoveTree(Stage(w.content,aid));RemoveTree(Stage(Root(w,"cache"),aid));
        }
        // Reports are best effort and never turn a successful local commit into
        // a failed installation. The local journal remains the source of truth.
        cJSON *event=cJSON_CreateObject();cJSON_AddStringToObject(event,"event_id",(w.request+"_"+(ok?"installed":Cancelled()?"cancelled":"failed")).c_str());
        cJSON_AddStringToObject(event,"outcome",ok?"installed":Cancelled()?"cancelled":"failed");char *body=cJSON_PrintUnformatted(event);cJSON *ignored=NULL;std::string ignoredError;
        Call(std::string(API)+"/install-requests/"+w.request+"/events",body,true,&ignored,&ignoredError);cJSON_free(body);cJSON_Delete(event);cJSON_Delete(ignored);
        Finish(ok,error);
    }
    return 0;
}

bool StartFoundry(const char *gamesRoot,const char *contentRoot)
{
    if(g_started)return true;InitializeCriticalSection(&g_lock);LoadDevice();g_wake=CreateEvent(NULL,FALSE,FALSE,NULL);
    if(!g_wake)return false;
    g_thread=CreateThread(NULL,512*1024,Worker,NULL,CREATE_SUSPENDED,NULL);if(!g_thread){CloseHandle(g_wake);return false;}
    g_started=true;ResumeThread(g_thread);
    cJSON *job=ReadJson(JOB,4096);
    if(job)
    {
        Work w;w.request=Text(job,"request");w.name=Text(job,"name");w.games=Text(job,"games");w.content=Text(job,"content");cJSON_Delete(job);
        if(FoundryHex(w.request,32)&&RootValid(w.games)&&RootValid(w.content))
        {EnterCriticalSection(&g_lock);g_work=w;InitSnap(w);InterlockedExchange(&g_cancel,0);g_pending=true;LeaveCriticalSection(&g_lock);SetEvent(g_wake);}
    }
    return true;
}
void StopFoundry(DWORD timeoutMs)
{
    if(!g_started)return;InterlockedExchange(&g_stop,1);InterlockedExchange(&g_cancel,1);SetEvent(g_wake);
    if(WaitForSingleObject(g_thread,timeoutMs)==WAIT_OBJECT_0){CloseHandle(g_thread);CloseHandle(g_wake);g_started=false;}
}
int SnapshotFoundryJobs(QueueJobSnapshot *out,int maximum)
{if(!g_started||maximum<1)return 0;EnterCriticalSection(&g_lock);int n=g_used?1:0;if(n)*out=g_snap;LeaveCriticalSection(&g_lock);return n;}
bool TakeFinishedFoundryJob(QueueJobSnapshot *out)
{if(!g_started)return false;EnterCriticalSection(&g_lock);bool ready=g_used&&g_snap.state==QUEUE_FINISHED&&!g_delivered;if(ready){*out=g_snap;g_delivered=true;}LeaveCriticalSection(&g_lock);return ready;}
bool IsFoundryJobId(int id){return id==JOB_ID;}
void CancelFoundryJob(int){InterlockedExchange(&g_cancel,1);}
void RemoveFoundryJob(int){EnterCriticalSection(&g_lock);if(g_snap.state==QUEUE_FINISHED)g_used=false;LeaveCriticalSection(&g_lock);}

static void PairUI()
{
    cJSON *reply=NULL;if(!UiCall(std::string(API)+"/pairings","{\"name\":\"Xbox 360\"}",false,&reply))return;
    std::string secret=Text(reply,"device_code"),code=Text(reply,"user_code"),url=Text(reply,"verification_uri");cJSON_Delete(reply);
    if(secret.size()<32||code.size()!=9){ShowMessageUI("Pairing failed","Invalid pairing response",NULL);return;}
    std::string detail=url+"  Code: "+code;
    if(!ShowConfirmUI("Pair Foundry","Open the website on your phone or computer and approve this code.",detail.c_str(),"I approved it"))return;
    cJSON *body=cJSON_CreateObject();cJSON_AddStringToObject(body,"device_code",secret.c_str());char *raw=cJSON_PrintUnformatted(body);
    if(UiCall(std::string(API)+"/pairings/token",raw,false,&reply))
    {
        std::string token=Text(reply,"access_token"),id=Text(reply,"device_id");
        if(Text(reply,"status")=="approved"&&token.size()>=40&&token.size()<100&&FoundryHex(id,32))
        {
            EnterCriticalSection(&g_lock);g_device.token=token;g_device.id=id;g_device.sequence=0;bool saved=SaveDevice();LeaveCriticalSection(&g_lock);
            ShowMessageUI("Foundry",saved?"Console paired":"Could not save the device token",NULL);
        }
        else ShowMessageUI("Not approved yet","Approve the code on the website and start pairing again.",NULL);
        cJSON_Delete(reply);
    }
    cJSON_free(raw);cJSON_Delete(body);
}
static void QueueRelease(const std::string &release,const std::string &name,const char *games,const char *content,bool digital)
{
    QueueJobSnapshot snap;if(SnapshotFoundryJobs(&snap,1)&&snap.state!=QUEUE_FINISHED){ShowMessageUI("Foundry","One Foundry installation is already active","Open Queue to see progress.");return;}
    const char *modes[]={"Convert on server","Convert on Xbox"};const char *details[]={"Download prepared and checked GoD files","Download the ISO archive, then convert locally"};
    int mode=digital?0:ShowChoiceUI("Conversion","Choose where this game is prepared",modes,details,2,L"Choose");if(mode<0)return;
    cJSON *body=cJSON_CreateObject();cJSON_AddStringToObject(body,"release_id",release.c_str());cJSON_AddStringToObject(body,"conversion",mode?"console":"server");
    cJSON *reply=NULL;std::string error;
    if(mode==0)
    {
        cJSON *info=NULL;
        if(!UiCall(std::string(API)+"/releases/"+release,"",true,&info)){cJSON_Delete(body);return;}
        const cJSON *components=cJSON_GetObjectItemCaseSensitive(info,"components");
        int count=cJSON_IsArray(components)?cJSON_GetArraySize(components):0;
        if(count>0&&count<=100)
        {
            std::vector<bool> selected(count,false);std::vector<std::string> ids,kinds,names;
            for(int i=0;i<count;++i)
            {
                const cJSON *c=cJSON_GetArrayItem(components,i);ids.push_back(Text(c,"component_id"));kinds.push_back(Text(c,"kind"));names.push_back(Text(c,"name"));
                selected[i]=digital?(kinds[i]=="dlc"||kinds[i]=="xbla"):(kinds[i]=="god"||kinds[i]=="xbla");
            }
            for(;;)
            {
                std::vector<std::string> labels;labels.push_back("Continue with selected components");
                for(int i=0;i<count;++i)labels.push_back(std::string(selected[i]?"[x] ":"[ ] ")+kinds[i]+" - "+names[i]);
                std::vector<const char*> pointers;for(size_t i=0;i<labels.size();++i)pointers.push_back(labels[i].c_str());
                int pick=ShowChoiceUI("Choose components","Select DLC and updates explicitly; updates must match your installed game",&pointers[0],NULL,(int)pointers.size(),L"Toggle / continue");
                if(pick<0){cJSON_Delete(info);cJSON_Delete(body);return;}
                if(pick==0)break;selected[pick-1]=!selected[pick-1];
            }
            cJSON *chosen=cJSON_AddArrayToObject(body,"component_ids");bool any=false;
            for(int i=0;i<count;++i)if(selected[i]){cJSON_AddItemToArray(chosen,cJSON_CreateString(ids[i].c_str()));any=true;}
            if(!any){cJSON_Delete(info);cJSON_Delete(body);ShowMessageUI("Nothing selected","Choose at least one component",NULL);return;}
        }
        cJSON_Delete(info);
    }
    EnterCriticalSection(&g_lock);++g_device.sequence;bool saved=SaveDevice();std::string idem=g_device.id+"_"+Decimal(g_device.sequence);LeaveCriticalSection(&g_lock);
    if(!saved){cJSON_Delete(body);ShowMessageUI("Not queued","Could not save the request counter",NULL);return;}
    // Install requests require an idempotency header in addition to bearer auth.
    Device d=DeviceCopy();std::string headers="Authorization: Bearer "+d.token+"\r\nIdempotency-Key: "+idem+"\r\n";
    // The UI task below uses the same bounded background transport, with this
    // request's extra header supplied through a JSON field-free helper.
    char *raw=cJSON_PrintUnformatted(body);
    // Queue creation is performed by UiCallWithKey (defined below).
    extern bool FoundryUiCreate(const std::string &,const std::string &,cJSON **);
    bool ok=FoundryUiCreate(raw,idem,&reply);cJSON_free(raw);cJSON_Delete(body);
    if(!ok)return;
    Work w;w.request=Text(reply,"id");w.name=name;w.games=games;w.content=content;cJSON_Delete(reply);
    if(!FoundryHex(w.request,32)||!RootValid(w.games)||!RootValid(w.content)||!SaveWork(w)){ShowMessageUI("Not queued","Could not save the installation request",NULL);return;}
    EnterCriticalSection(&g_lock);g_work=w;InitSnap(w);InterlockedExchange(&g_cancel,0);g_pending=true;LeaveCriticalSection(&g_lock);SetEvent(g_wake);
    ShowMessageUI("Queued in Foundry","Installation continues in the background","Open Queue for progress. B returns to the original store.");
}

// The idempotency key belongs to the request, not to a global credential.
struct CreateTask{std::string body,key,error;bool ok;cJSON *reply;CreateTask():ok(false),reply(NULL){}};
static DWORD WINAPI CreateEntry(LPVOID p)
{
    CreateTask *t=(CreateTask*)p;Device d=DeviceCopy();std::string headers="Authorization: Bearer "+d.token+"\r\nIdempotency-Key: "+t->key+"\r\n";
    HttpResponsePolicy policy;policy.json=true;std::vector<char> buffer(65537);unsigned long long n=65536;
    int status=httpRequestHTTPS(d.origin+API+"/install-requests",HTTP_POST,t->body.c_str(),headers.c_str(),"",&buffer[0],&n,false,NULL,0,dprintf,0,UiProgress,false,&policy);
    if(n>65536)n=65536;buffer[(size_t)n]=0;t->reply=cJSON_Parse(&buffer[0]);t->ok=status==200&&cJSON_IsObject(t->reply);t->error=Text(t->reply,"error");return 0;
}
bool FoundryUiCreate(const std::string &body,const std::string &key,cJSON **reply)
{
    CreateTask t;t.body=body;t.key=key;InterlockedExchange(&g_uiCancel,0);HANDLE thread=CreateThread(NULL,256*1024,CreateEntry,&t,0,NULL);if(!thread)return false;
    while(WaitForSingleObject(thread,0)!=WAIT_OBJECT_0){RenderProgressFrame("Foundry","Creating installation request",NULL,-1,"Preparing");if(PollUiInput().pressed&XINPUT_GAMEPAD_B)InterlockedExchange(&g_uiCancel,1);Sleep(16);}
    CloseHandle(thread);ResyncUiInput();if(!t.ok){ShowMessageUI("Not queued",t.error.empty()?"Server request failed":t.error.c_str(),NULL);cJSON_Delete(t.reply);return false;}*reply=t.reply;return true;
}
static void BrowseUI(const char *games,const char *content,bool digital)
{
    std::string query;OpenKeyboardToString(0,&query,L"Search Foundry",L"Game or add-on name",L"");std::string cursor;
    for(;;)
    {
        cJSON *reply=NULL;std::string path=std::string(API)+"/catalog?kind="+(digital?"digital":"game")+"&limit=30&q="+Encode(query)+(cursor.empty()?"":"&cursor="+cursor);
        if(!UiCall(path,"",true,&reply))return;const cJSON *items=cJSON_GetObjectItemCaseSensitive(reply,"items");
        std::vector<std::string> labels,subs,releases;
        for(int i=0;cJSON_IsArray(items)&&i<cJSON_GetArraySize(items)&&i<30;++i)
        {const cJSON *item=cJSON_GetArrayItem(items,i);labels.push_back(Text(item,"name"));subs.push_back(Text(item,"availability"));releases.push_back(Text(item,"release_id"));}
        std::string next=Text(reply,"next_cursor");cJSON_Delete(reply);int count=(int)labels.size();
        if(!next.empty()){labels.push_back("Next page");subs.push_back("");}
        if(labels.empty()){ShowMessageUI("Foundry","No matching releases",NULL);return;}
        std::vector<const char*> names,details;for(size_t i=0;i<labels.size();++i){names.push_back(labels[i].c_str());details.push_back(subs[i].c_str());}
        int pick=ShowChoiceUI("Foundry",digital?"XBLA / DLC sources":"Xbox 360 games",&names[0],&details[0],(int)names.size(),L"Install");
        if(pick<0)return;if(pick==count){cursor=next;continue;}
        QueueRelease(releases[pick],labels[pick],games,content,digital);return;
    }
}
static void OnlineUpdatesUI(const char *games,const char *content)
{
    cJSON *caps=NULL;if(!UiCall(std::string(API)+"/capabilities","",false,&caps))return;
    bool available=cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(caps,"online_title_updates"));cJSON_Delete(caps);
    if(!available){ShowMessageUI("Title updates","This server does not enable online title-update requests",NULL);return;}
    QueueJobSnapshot snap;if(SnapshotFoundryJobs(&snap,1)&&snap.state!=QUEUE_FINISHED){ShowMessageUI("Foundry","Wait for the active installation to finish",NULL);return;}
    RenderProgressFrame("Title updates","Reading installed game execution IDs",NULL,-1,"Checking library");
    std::vector<InstalledGame> installed(256);int found=EnumerateInstalledGames(games,&installed[0],(int)installed.size(),dprintf);
    std::vector<InstalledIdentity> ids;std::vector<std::string> labels;
    for(int i=0;i<found&&ids.size()<256;++i)
    {
        std::string dir=std::string(games)+"\\"+Hex32(installed[i].titleId)+"\\00007000";WIN32_FIND_DATAA fd;HANDLE h=FindFirstFileA((dir+"\\*").c_str(),&fd);
        if(h==INVALID_HANDLE_VALUE)continue;
        do {
            if(fd.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY)continue;
            InstalledGodSource source(dir+"\\"+fd.cFileName);GodImageInfo info;
            if(GodInspect(&source,&info)!=GOD_OK||info.title.contentType!=GOD_CONTENT_GAMES_ON_DEMAND)continue;
            InstalledIdentity id;id.title=Hex32(info.title.titleId);id.media=Hex32(info.title.mediaId);id.base=Hex32(info.title.baseVersion);
            bool duplicate=false;for(size_t j=0;j<ids.size();++j)if(ids[j].title==id.title&&ids[j].media==id.media&&ids[j].base==id.base)duplicate=true;
            if(!duplicate&&ids.size()<256){ids.push_back(id);labels.push_back(std::string(installed[i].displayName)+" [Media "+id.media+"]");}
        }while(FindNextFileA(h,&fd));FindClose(h);
    }
    FreeInstalledGames(&installed[0],found);ResyncUiInput();
    if(ids.empty()){ShowMessageUI("Title updates","No installed Xbox 360 GoD game with a readable execution ID",NULL);return;}
    std::vector<const char*> names;for(size_t i=0;i<labels.size();++i)names.push_back(labels[i].c_str());
    int pick=ShowChoiceUI("Title updates","Find the latest exact match on XboxUnity through Foundry",&names[0],NULL,(int)names.size(),L"Find and install");if(pick<0)return;
    cJSON *o=cJSON_CreateObject(),*identity=cJSON_AddObjectToObject(o,"game_identity");cJSON_AddStringToObject(o,"kind","title_update");
    cJSON_AddStringToObject(identity,"title_id",ids[pick].title.c_str());cJSON_AddStringToObject(identity,"media_id",ids[pick].media.c_str());cJSON_AddStringToObject(identity,"base_version",ids[pick].base.c_str());
    EnterCriticalSection(&g_lock);++g_device.sequence;bool saved=SaveDevice();std::string key=g_device.id+"_"+Decimal(g_device.sequence);LeaveCriticalSection(&g_lock);
    if(!saved){cJSON_Delete(o);ShowMessageUI("Not queued","Could not save the request counter",NULL);return;}
    char *raw=cJSON_PrintUnformatted(o);cJSON *reply=NULL;bool ok=FoundryUiCreate(raw,key,&reply);cJSON_free(raw);cJSON_Delete(o);if(!ok)return;
    Work w;w.request=Text(reply,"id");w.name=labels[pick]+" - title update";w.games=games;w.content=content;cJSON_Delete(reply);
    if(!FoundryHex(w.request,32)||!SaveWork(w)){ShowMessageUI("Not queued","Could not save the update request",NULL);return;}
    EnterCriticalSection(&g_lock);g_work=w;InitSnap(w);InterlockedExchange(&g_cancel,0);g_pending=true;LeaveCriticalSection(&g_lock);SetEvent(g_wake);
    ShowMessageUI("Title update queued","Foundry will download and check the exact matching update","Enable it in Aurora after installation. No approximate update will be installed.");
}
void OpenFoundryUI(const char *games,const char *content)
{
    if(!g_started&&!StartFoundry(games,content)){ShowMessageUI("Foundry","Worker could not start",NULL);return;}
    for(;;)
    {
        Device d=DeviceCopy();const char *names[]={"Search games","Search XBLA / DLC","Pair this console","Server address","Resume interrupted installation","Forget console token","Title updates for installed games"};
        const char *subs[]={"Server or Xbox conversion","Install prepared digital content",d.token.empty()?"Not paired":"Paired",d.origin.c_str(),"Checked files are reused","The website can also revoke access","Exact execution ID match, including Xbox-converted games"};
        int choice=ShowChoiceUI("GOD Foundry","Your server library on the Xbox",names,subs,7,L"Open");if(choice<0)return;
        if(choice==2){PairUI();continue;}
        if(choice==3)
        {
            std::string typed;if(OpenKeyboardToString(0,&typed,L"Foundry server",L"HTTPS origin, with no trailing path",L"https://iso2god.mediahub.stream")!=ERROR_SUCCESS)continue;
            if(!typed.empty()&&typed[typed.size()-1]=='/')typed.resize(typed.size()-1);
            if(!FoundryOrigin(typed)){ShowMessageUI("Invalid address","Use an HTTPS hostname without a path or port",NULL);continue;}
            EnterCriticalSection(&g_lock);g_device.origin=typed;g_device.token.clear();g_device.id.clear();SaveDevice();LeaveCriticalSection(&g_lock);continue;
        }
        if(choice==5){if(ShowConfirmUI("Forget Foundry access","This removes the token from this Xbox","Revoke the device on the website to invalidate any copies.","Forget")){EnterCriticalSection(&g_lock);g_device.token.clear();g_device.id.clear();SaveDevice();LeaveCriticalSection(&g_lock);}continue;}
        if(d.token.empty()){PairUI();continue;}
        if(choice==6){OnlineUpdatesUI(games,content);continue;}
        if(choice==4)
        {
            QueueJobSnapshot snap;bool active=SnapshotFoundryJobs(&snap,1)&&snap.state!=QUEUE_FINISHED;
            if(Exists(JOB)&&!active){EnterCriticalSection(&g_lock);InitSnap(g_work);InterlockedExchange(&g_cancel,0);g_pending=true;LeaveCriticalSection(&g_lock);SetEvent(g_wake);ShowMessageUI("Resuming","Open Queue for progress",NULL);}
            else ShowMessageUI("Foundry",active?"An installation is already active":"No interrupted installation",NULL);
        }
        else BrowseUI(games,content,choice==1);
    }
}
