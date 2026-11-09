// Omni360 GoD Foundry integration, 2026. AGPL-3.0; see LICENSE.
#include "Foundry.h"
#include "FoundryProtocol.h"
#include "AvatarPackage.h"
#include "ContentWriteLock.h"
#include "GameInstaller.h"
#include "DiscWorker.h"
#include "GameListUI.h"
#include "Keyboard.h"
#include "downloadFile.h"
#include "XboxTLS.h"
#include "GodConvert.h"
#include "InflateSource.h"
#include "ReadAhead.h"
#include "StfsParser.h"
#include "OutputConsole.h"
#include "cJSON.h"
#include "inc\\bearssl.h"
#include "../zlib/zlib.h"
#include <xtl.h>
#ifdef min
#undef min
#endif
#ifdef max
#undef max
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <algorithm>
#include <map>
#include <set>
#include <ctype.h>

static const char *API="/api/console/v1";
static const char *DEVICE="game:\\FoundryDevice.json";
static const char *JOB="game:\\FoundryJob.json";
static const char *TXN="game:\\FoundryTxn.json";
static const char *INBOX="game:\\FoundryInbox.json";
static const unsigned long long PIECE=32ULL*1024*1024;
static const int JOB_ID=3000000;

struct Device
{
    std::string origin, token, id, mode;
    unsigned long long sequence;
    bool autoInbox;
    Device():origin("https://iso2god.mediahub.stream"),mode("server"),sequence(0),autoInbox(false){}
};
struct Work
{
    std::string request, name, games, content, body, key, origin, device;
    bool inbox, addon;
    unsigned long art;
    Work():inbox(false),addon(false),art(0){}
};
struct Move
{
    std::string root, from, to;
};
struct InstalledIdentity { std::string title, media, base; };
static Device g_device;
static Work g_work;
struct FinishedWork { Work work;QueueJobSnapshot snap; };
static std::vector<Work> g_waiting;
static std::vector<FinishedWork> g_finished;
static std::map<std::string,int> g_queueIds;
static int g_nextQueueId=JOB_ID;
static bool g_inboxLoaded=false;
static std::string WorkKey(const Work &w){return w.origin+"\n"+w.device+"\n"+(w.request.empty()?w.key:w.request);}
static int WorkId(const Work &w){std::string key=WorkKey(w);std::map<std::string,int>::iterator it=g_queueIds.find(key);if(it!=g_queueIds.end())return it->second;return g_queueIds[key]=g_nextQueueId++;}
static bool SaveInbox(const std::vector<Work> &queue,const std::string &completed=std::string());
static bool LoadInbox(std::vector<Work> *queue);
static void SnapWork(const Work &w,QueueJobSnapshot *snap)
{
    memset(snap,0,sizeof(*snap));snap->id=WorkId(w);snap->kind=w.addon?QUEUE_JOB_DLC_PACK:QUEUE_JOB_GAME_INSTALL;snap->state=QUEUE_WAITING;snap->fraction=-1;snap->titleId=w.art;
    _snprintf(snap->gameName,sizeof(snap->gameName),"%s",w.name.c_str());_snprintf(snap->title,sizeof(snap->title),"%s",w.name.c_str());
    snap->gameName[sizeof(snap->gameName)-1]=0;snap->title[sizeof(snap->title)-1]=0;
}
static bool GameBeforeAddon(const Work &a,const Work &b){return !a.addon&&b.addon;}
static CRITICAL_SECTION g_lock;
static HANDLE g_thread=NULL, g_wake=NULL;
static volatile LONG g_stop=0, g_cancel=0, g_uiCancel=0;
static bool g_started=false,g_pending=false,g_used=false,g_delivered=false;
static QueueJobSnapshot g_snap;
static unsigned long long g_transferBase=0,g_transferTotal=0;
static unsigned long long g_transferJobDone=0,g_transferJobTotal=0;
static DWORD g_conversionStarted=0;
static unsigned g_transferFile=0,g_transferFiles=0;
static std::string g_transferName;
static std::string g_defaultGames,g_defaultContent;
struct StoreCacheEntry { std::string key,json;DWORD fetched,used; };
static std::vector<StoreCacheEntry> g_storeCache;
static size_t g_storeCacheBytes=0;
static unsigned g_storeCacheGeneration=0;
static HANDLE g_storePrefetch=NULL;
static std::string g_storePrefetchKey;
static volatile LONG g_storePrefetchCancel=0;
// Each lane has exactly one owner. Never share a live TLS context between the
// foreground UI request and the background preload, or with the installer.
static HttpsSession *g_storeSession=NULL,*g_storePrefetchSession=NULL;
static std::string g_storeSessionOwner,g_storePrefetchOwner;
static std::string g_storePrefetchAttemptKey;
static DWORD g_storePrefetchAttemptTime=0;
static const DWORD STORE_CACHE_TTL=5*60*1000;

static std::string Text(const cJSON *o,const char *k)
{
    const cJSON *v=cJSON_GetObjectItemCaseSensitive(o,k);
    return cJSON_IsString(v)&&v->valuestring?v->valuestring:"";
}
static std::string Decimal(unsigned long long n)
{
    char s[32]; _snprintf(s,sizeof(s),"%I64u",n); s[sizeof(s)-1]=0; return s;
}
static bool Exists(const std::string &p) { return GetFileAttributesA(p.c_str())!=(DWORD)-1; }
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
    cJSON_AddBoolToObject(o,"auto_inbox",g_device.autoInbox);
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
    FoundryDecimal(Text(o,"sequence").c_str(),&g_device.sequence);g_device.autoInbox=cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(o,"auto_inbox")); cJSON_Delete(o);
}
static Device DeviceCopy() { EnterCriticalSection(&g_lock);Device d=g_device;LeaveCriticalSection(&g_lock);return d; }
static void Phase(const char *name,float fraction=-1.0f,unsigned long long done=0,unsigned long long total=0)
{
    EnterCriticalSection(&g_lock);
    _snprintf(g_snap.phase,sizeof(g_snap.phase),"%s",name); g_snap.phase[sizeof(g_snap.phase)-1]=0;
    g_snap.fraction=fraction;g_snap.bytesDone=done;g_snap.bytesTotal=total;
    g_snap.bytesPerSec=0;g_snap.secondsLeft=0;
    LeaveCriticalSection(&g_lock);
}
static bool Cancelled() { return InterlockedCompareExchange(&g_cancel,0,0)||InterlockedCompareExchange(&g_stop,0,0); }
static bool DownloadProgress(unsigned long long done,unsigned long long total,unsigned long long speed,unsigned long long left)
{
    done+=g_transferBase;if(g_transferTotal)total=g_transferTotal;
    if(g_transferJobTotal){done+=g_transferJobDone;total=g_transferJobTotal;left=speed&&total>done?(total-done)/speed+((total-done)%speed!=0):0;}
    EnterCriticalSection(&g_lock);g_snap.bytesDone=done;g_snap.bytesTotal=total;g_snap.bytesPerSec=speed;g_snap.secondsLeft=left;
    g_snap.fraction=total?(float)((double)done/total):-1.0f;LeaveCriticalSection(&g_lock);return !Cancelled();
}
static void TransferFilePhase(bool retry)
{
    if(!g_transferFiles)return;
    char phase[64];_snprintf(phase,sizeof(phase),"%s %u/%u (%u left): %.22s",retry?"Retry file":"File",g_transferFile,g_transferFiles,g_transferFiles-g_transferFile,g_transferName.c_str());phase[sizeof(phase)-1]=0;
    EnterCriticalSection(&g_lock);strcpy(g_snap.phase,phase);LeaveCriticalSection(&g_lock);
}
static void BeginTransferFile(unsigned index,unsigned count,unsigned long long done,unsigned long long total,const std::string &name)
{
    g_transferFile=index;g_transferFiles=count;g_transferJobDone=done;g_transferJobTotal=total;g_transferName=name;
    g_transferBase=0;g_transferTotal=0;TransferFilePhase(false);DownloadProgress(0,0,0,0);
}
static bool UiProgress(unsigned long long,unsigned long long,unsigned long long,unsigned long long)
{ return !InterlockedCompareExchange(&g_uiCancel,0,0); }

static bool RetryPause(unsigned attempt,bool ui=false)
{
    unsigned ticks=std::min(300U,10U*(attempt+1)*(attempt+1));
    for(unsigned i=0;i<ticks;++i)
    {
        if(ui?InterlockedCompareExchange(&g_uiCancel,0,0)!=0:Cancelled())return false;
        Sleep(100);
    }
    return true;
}

static bool Call(const std::string &path,const std::string &body,bool auth,cJSON **reply,std::string *error,bool ui=false,const std::string &key="",int *lastStatus=NULL,unsigned attempts=6,const Device *boundDevice=NULL,DownloadProgressFn progress=NULL,HttpsSession **session=NULL,std::string *sessionOwner=NULL)
{
    Device d=boundDevice?*boundDevice:DeviceCopy();
    if(!FoundryOrigin(d.origin)||path.compare(0,strlen(API),API)!=0||path.find_first_of("\r\n")!=std::string::npos)
    { *error="Invalid Foundry URL";return false; }
    std::string headers=auth?"Authorization: Bearer "+d.token+"\r\n":"";
    if(!key.empty()) headers+="Idempotency-Key: "+key+"\r\n";
    HttpResponsePolicy policy;policy.json=true;
    // Only read-only Store metadata uses keep-alive. Durable POSTs, manifests,
    // ranges and download recovery keep their existing transport and checks.
    if(session && (!sessionOwner || !body.empty() || path.compare(0,strlen(API)+7,std::string(API)+"/store/")!=0))session=NULL;
    if(session)
    {
        std::string owner=d.origin+"\n"+d.id+"\n"+d.token;
        if(*sessionOwner!=owner){HttpsSessionClose(*session);*session=NULL;*sessionOwner=owner;}
        if(!*session)*session=HttpsSessionOpen(dprintf);
    }
    std::vector<char> buffer(1024*1024+1);unsigned long long len=0;int status=0;
    for(unsigned attempt=0;attempt<attempts;++attempt)
    {
        std::fill(buffer.begin(),buffer.end(),0);len=buffer.size()-1;
        DownloadProgressFn callback=progress?progress:ui?UiProgress:NULL;
        status=session&&*session?HttpsSessionGet(*session,d.origin+path,headers.c_str(),&buffer[0],&len,true,callback):
            httpRequestHTTPS(d.origin+path,body.empty()?HTTP_GET:HTTP_POST,body.empty()?NULL:body.c_str(),headers.c_str(),"",
                &buffer[0],&len,false,NULL,0,dprintf,0,callback,false,&policy);
        if(!FoundryRetryableStatus(status)||attempt+1==attempts||!RetryPause(attempt,ui))break;
    }
    if(lastStatus)*lastStatus=status;
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
{
    UiTask *t=(UiTask*)p;bool catalog=t->body.empty()&&t->path.compare(0,strlen(API)+7,std::string(API)+"/store/")==0;
    if(catalog)XboxTLS_SetThreadIoTimeout(15000);
    t->ok=Call(t->path,t->body,t->auth,&t->reply,&t->error,true,"",NULL,catalog?2:6,NULL,NULL,
        catalog?&g_storeSession:NULL,catalog?&g_storeSessionOwner:NULL);return 0;
}
static bool UiCall(const std::string &path,const std::string &body,bool auth,cJSON **reply)
{
    UiTask task;task.path=path;task.body=body;task.auth=auth;InterlockedExchange(&g_uiCancel,0);
    HANDLE thread=CreateThread(NULL,256*1024,UiTaskEntry,&task,0,NULL);
    if(!thread) { ShowMessageUI("Foundry","Could not start the network request",NULL);return false; }
    DWORD started=GetTickCount();bool catalog=body.empty()&&path.compare(0,strlen(API)+7,std::string(API)+"/store/")==0;
    while(WaitForSingleObject(thread,0)!=WAIT_OBJECT_0)
    {
        if(GetTickCount()-started>=150)
            RenderProgressFrame("Foundry",catalog?"Loading catalog page":"Contacting your server","B cancels this request",-1,catalog?"Loading":"Connecting");
        if(PollUiInput().pressed&XINPUT_GAMEPAD_B) InterlockedExchange(&g_uiCancel,1);
        Sleep(16);
    }
    CloseHandle(thread);ResyncUiInput();
    if(!task.ok) { if(!InterlockedCompareExchange(&g_uiCancel,0,0))ShowMessageUI("Foundry",task.error.c_str(),NULL);return false; }
    *reply=task.reply;return true;
}

// Catalog-only, short-lived and bounded. Install requests/manifests/files never
// use this cache. A device/server change cannot reuse another account's pages.
static std::string StoreCacheKey(const Device &d,const std::string &path)
{return d.origin+"\n"+d.id+"\n"+d.token+"\n"+path;}
static bool StoreCacheRead(const std::string &key,cJSON **reply)
{
    DWORD now=GetTickCount();std::string raw;bool found=false;
    EnterCriticalSection(&g_lock);
    for(size_t i=0;i<g_storeCache.size();)
    {
        StoreCacheEntry &e=g_storeCache[i];
        if(now-e.fetched>=STORE_CACHE_TTL){g_storeCacheBytes-=e.json.size();g_storeCache.erase(g_storeCache.begin()+i);continue;}
        if(e.key==key){e.used=now;if(reply)raw=e.json;found=true;}
        ++i;
    }
    LeaveCriticalSection(&g_lock);
    if(!found)return false;if(!reply)return true;
    *reply=cJSON_Parse(raw.c_str());return cJSON_IsObject(*reply)!=0;
}
static void StoreCacheSave(const std::string &key,const cJSON *reply,unsigned generation)
{
    char *json=cJSON_PrintUnformatted(reply);if(!json)return;std::string raw=json;cJSON_free(json);
    if(raw.size()>512*1024)return;
    DWORD now=GetTickCount();EnterCriticalSection(&g_lock);
    if(generation!=g_storeCacheGeneration){LeaveCriticalSection(&g_lock);return;}
    for(size_t i=0;i<g_storeCache.size();++i)if(g_storeCache[i].key==key){g_storeCacheBytes-=g_storeCache[i].json.size();g_storeCache.erase(g_storeCache.begin()+i);break;}
    while(!g_storeCache.empty()&&(g_storeCache.size()>=16||g_storeCacheBytes+raw.size()>2*1024*1024))
    {size_t oldest=0;for(size_t i=1;i<g_storeCache.size();++i)if(now-g_storeCache[i].used>now-g_storeCache[oldest].used)oldest=i;
        g_storeCacheBytes-=g_storeCache[oldest].json.size();g_storeCache.erase(g_storeCache.begin()+oldest);}
    StoreCacheEntry e;e.key=key;e.json=raw;e.fetched=e.used=now;g_storeCache.push_back(e);g_storeCacheBytes+=raw.size();LeaveCriticalSection(&g_lock);
}
void ClearFoundryStoreCache()
{EnterCriticalSection(&g_lock);++g_storeCacheGeneration;g_storeCache.clear();g_storeCacheBytes=0;LeaveCriticalSection(&g_lock);g_storePrefetchAttemptKey.clear();InterlockedExchange(&g_storePrefetchCancel,1);}
struct StorePrefetchTask { Device device;std::string path,key;unsigned generation; };
static bool StorePrefetchProgress(unsigned long long,unsigned long long,unsigned long long,unsigned long long)
{return !InterlockedCompareExchange(&g_storePrefetchCancel,0,0)&&!InterlockedCompareExchange(&g_stop,0,0);}
static DWORD WINAPI StorePrefetchEntry(LPVOID value)
{
    StorePrefetchTask *task=(StorePrefetchTask*)value;cJSON *reply=NULL;std::string error;
    XboxTLS_SetThreadIoTimeout(15000);
    if(StorePrefetchProgress(0,0,0,0)&&Call(std::string(API)+task->path,"",true,&reply,&error,false,"",NULL,1,&task->device,StorePrefetchProgress,&g_storePrefetchSession,&g_storePrefetchOwner))
        StoreCacheSave(task->key,reply,task->generation);
    cJSON_Delete(reply);delete task;return 0;
}
void PrefetchFoundryStore(const std::string &path)
{
    if(!g_started||g_stop||path.compare(0,7,"/store/")!=0)return;
    QueueJobSnapshot snap;if(SnapshotFoundryActiveJob(&snap)&&snap.state!=QUEUE_FINISHED)return; // Downloads have priority.
    Device d=DeviceCopy();if(d.token.empty())return;std::string key=StoreCacheKey(d,path);
    if(g_storePrefetch){if(WaitForSingleObject(g_storePrefetch,0)!=WAIT_OBJECT_0)return;CloseHandle(g_storePrefetch);g_storePrefetch=NULL;}
    if(StoreCacheRead(key,NULL))return;
    DWORD now=GetTickCount();if(key==g_storePrefetchAttemptKey&&now-g_storePrefetchAttemptTime<5000)return;
    g_storePrefetchAttemptKey=key;g_storePrefetchAttemptTime=now;
    StorePrefetchTask *task=new StorePrefetchTask;task->device=d;task->path=path;task->key=key;
    EnterCriticalSection(&g_lock);task->generation=g_storeCacheGeneration;LeaveCriticalSection(&g_lock);
    g_storePrefetchKey=key;InterlockedExchange(&g_storePrefetchCancel,0);
    g_storePrefetch=CreateThread(NULL,256*1024,StorePrefetchEntry,task,0,NULL);if(!g_storePrefetch)delete task;
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
    size_t got;while((got=fread(&block[0],1,block.size(),f))>0){if(Cancelled()){fclose(f);return false;}br_sha256_update(&sha,&block[0],got);n+=got;}
    bool ok=!ferror(f)&&(!expected||n==expected);fclose(f);
    unsigned char bytes[32];br_sha256_out(&sha,bytes);char text[65];
    for(int i=0;i<32;++i)sprintf(text+i*2,"%02x",bytes[i]);text[64]=0;*hash=text;return ok;
}
static unsigned long long Length(const std::string &path)
{
    WIN32_FILE_ATTRIBUTE_DATA d;if(!GetFileAttributesExA(path.c_str(),GetFileExInfoStandard,&d))return 0;
    return ((unsigned long long)d.nFileSizeHigh<<32)|d.nFileSizeLow;
}
static bool RangeDownload(const std::string &url,const std::string &partial,unsigned long long fullSize,
                          unsigned long long start,unsigned long long size,const std::string &sha,std::string *error)
{
    if(!size||start>fullSize||size>fullSize-start||!Parents(partial)){*error="Invalid download range or staging path";return false;}
    if(Length(partial)>size)DeleteFileA(partial.c_str());
    unsigned failures=0;int lastStatus=0;
    for(unsigned attempt=0;attempt<200 && failures<6;++attempt)
    {
        if(Cancelled()){*error="Paused; downloaded bytes are kept for resume";return false;}
        unsigned long long before=Length(partial);if(before==size)return true;
        Device d=DeviceCopy();std::string etag="\""+sha+"\"";
        std::string headers="Authorization: Bearer "+d.token+"\r\nRange: bytes="+Decimal(start+before)+"-"+Decimal(start+size-1)+"\r\nIf-Range: "+etag+"\r\n";
        std::string range="bytes "+Decimal(start+before)+"-"+Decimal(start+size-1)+"/"+Decimal(fullSize);
        HttpResponsePolicy policy;policy.status=206;policy.etag=etag.c_str();policy.contentRange=range.c_str();
        g_transferBase=before;g_transferTotal=size;
        TransferFilePhase(attempt!=0);
        int status=httpRequestHTTPS(d.origin+url,HTTP_GET,NULL,headers.c_str(),partial,NULL,NULL,true,NULL,0,dprintf,size-before,DownloadProgress,before!=0,&policy);
        unsigned long long after=Length(partial);
        if(after<before||after>size){*error="Download length changed unexpectedly";return false;}
        if(after==size)return true; // Full-file SHA-256/ZIP CRC is checked by the caller.
        if(status==HTTP_STATUS_CANCELLED||Cancelled()){*error="Paused; downloaded bytes are kept for resume";return false;}
        lastStatus=status;
        if(status!=206)dprintf("[foundry] resume HTTP %d at %I64u/%I64u for %s\n",status,start+before,fullSize,url.c_str());
        // A proxy can temporarily return 404 while its healthy app route is
        // being replaced. Retry this idempotent range without touching bytes.
        // Keep strict ETag/range/length checks; never append an HTML response.
        if(!FoundryRetryableStatus(status)&&status!=206&&status!=404){
            if(status==401||status==403)*error="Pair this console again";
            else{char message[256];_snprintf(message,sizeof(message),"Download stopped (HTTP %d). %s Checked and partial files are kept.",status,status==409||status==410?"Prepare this selection again on the website.":"Resume the interrupted installation when the server is available.");message[sizeof(message)-1]=0;*error=message;}
            return false;}
        failures=after>before?0:failures+1;
        if(!RetryPause(failures))break;
    }
    *error=lastStatus==404?"Download temporarily unavailable (HTTP 404). Resume the interrupted installation; checked and partial files are kept.":"Connection interrupted; partial download is saved for resume";return false;
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
    if(!RangeDownload(path,partial,bytes,0,bytes,sha,error))return false;
    if(Length(partial)<bytes){*error="Download interrupted; partial file kept for resume";return false;}
    if(!HashFile(partial,&hash,bytes)||hash!=sha)
    {
        if(Cancelled()){*error="Paused; downloaded file is kept for verification";return false;}
        DeleteFileA(partial.c_str());
        if(!RangeDownload(path,partial,bytes,0,bytes,sha,error))return false;
        if(!HashFile(partial,&hash,bytes)||hash!=sha){if(!Cancelled())DeleteFileA(partial.c_str());*error="Downloaded file failed its SHA-256 check";return false;}
    }
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
    unsigned long size=BE32(&header[0x340]);
    if(size<0x971a||size>0x100000){fclose(f);*error="Invalid metadata length";return false;}
    unsigned long end=(size+0xfff)&~0xfff;
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
        if(c.discNumber)ok=ok&&header[0x366]==c.discNumber;
        if(c.discCount)ok=ok&&header[0x367]==c.discCount;
    }
    if(c.kind=="god"||c.kind=="title_update")ok=ok&&Hex32(BE32(&header[0x354]))==c.mediaId;
    if(c.kind=="title_update")ok=ok&&Hex32(BE32(&header[0x35c]))==c.baseVersion;
    if(!ok){*error="Package metadata does not match the installation";return false;}
    return true;
}
static std::vector<unsigned long long> AvatarProfileXuids()
{
    std::vector<unsigned long long> out;
    for(DWORD user=0;user<4;++user)for(DWORD flag=1;flag<=2;++flag){XUSER_SIGNIN_INFO info;memset(&info,0,sizeof(info));
        if(XUserGetSigninInfo(user,flag,&info)==ERROR_SUCCESS&&info.UserSigninState!=eXUserSigninState_NotSignedIn&&AvatarOnlineXuid(info.xuid)&&std::find(out.begin(),out.end(),info.xuid)==out.end())out.push_back(info.xuid);}
    return out;
}
bool FoundryCanUnlockAvatars(){return !AvatarProfileXuids().empty();}
static bool PatchAvatarForInstall(const Work &w,const std::string &aid,FoundryComponent *component,std::string *error)
{
    if(component->kind!="avatar"||!component->avatarUnlock)return true;
    std::vector<unsigned long long> xuids=AvatarProfileXuids();
    if(xuids.empty()){*error="Sign in with a Live-enabled or activated offline profile to unlock Avatar items. Your downloaded files are kept. Omni360 does not change profiles.";return false;}
    std::string source=Stage(w.content,aid)+"\\files\\"+Native(component->files[0].path);
    FILE *in=fopen(source.c_str(),"rb");if(!in){*error="Avatar source is missing";return false;}
    unsigned char first[0x344];bool ok=fread(first,1,sizeof(first),in)==sizeof(first);unsigned size=ok?BE32(first+0x340):0,end=(size+0xFFF)&~0xFFF;
    if(!ok||size<0x971A||size>0x100000||end>Length(source)){fclose(in);*error="Invalid Avatar metadata length";return false;}
    std::vector<unsigned char> header(end);fseek(in,0,SEEK_SET);ok=fread(&header[0],1,end,in)==end;
    std::string relative,contentId;if(!ok||!AvatarPatchHeader(&header,xuids,&relative,&contentId,error)){fclose(in);return false;}
    // Keep previous profile licenses when replacing an installed item.
    std::string existing=w.content+"\\"+Native(relative);
    if(Exists(existing)){
        FoundryComponent expected;expected.kind="avatar";expected.titleId="AAAAAAAA";expected.contentType="00009000";
        if(!CheckPackage(existing,expected,error)){fclose(in);return false;}
        FILE *old=fopen(existing.c_str(),"rb");unsigned char oldHeader[0x3F1];bool read=old&&fread(oldHeader,1,sizeof(oldHeader),old)==sizeof(oldHeader);if(old)fclose(old);
        if(!read||memcmp(oldHeader+0x3E1,&header[0x3E1],16)){fclose(in);*error="Existing Avatar asset identity differs; nothing replaced";return false;}
        for(unsigned i=0;i<16;++i){unsigned long long x=(static_cast<unsigned long long>(BE32(oldHeader+0x22C+i*16))<<32)|BE32(oldHeader+0x230+i*16);
            if(AvatarOnlineXuid(x)&&std::find(xuids.begin(),xuids.end(),x)==xuids.end())xuids.push_back(x);}
        if(!AvatarPatchHeader(&header,xuids,&relative,&contentId,error)){fclose(in);return false;}
    }
    std::string target=Stage(w.content,aid)+"\\avatar\\"+Native(relative),temporary=target+".tmp";
    if(!Parents(target)){fclose(in);*error="Could not create Avatar staging folder";return false;}
    HANDLE out=CreateFileA(temporary.c_str(),GENERIC_WRITE,0,NULL,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,NULL);DWORD written=0;
    ok=out!=INVALID_HANDLE_VALUE&&WriteFile(out,&header[0],end,&written,NULL)&&written==end;
    unsigned char buffer[65536];size_t n=0;while(ok&&(n=fread(buffer,1,sizeof(buffer),in))>0){ok=!Cancelled()&&WriteFile(out,buffer,(DWORD)n,&written,NULL)&&written==n;}
    ok=ok&&!ferror(in);fclose(in);if(out!=INVALID_HANDLE_VALUE){ok=ok&&FlushFileBuffers(out);CloseHandle(out);}
    if(!ok){DeleteFileA(temporary.c_str());*error="Could not stage Avatar ownership; checked downloads are kept";return false;}
    DeleteFileA(target.c_str());if(!MoveFileA(temporary.c_str(),target.c_str())){*error="Could not publish staged Avatar item";return false;}
    component->titleId="AAAAAAAA";component->contentId=contentId;component->files[0].path=relative;
    component->files[0].installSource="_Foundry/"+aid+"/avatar/"+relative;
    return Length(target)==component->files[0].bytes&&CheckPackage(target,*component,error);
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
static bool JournalPath(const std::string &path,const std::string &root,const std::string &aid)
{
    if(!FoundrySafePath(path))return false;
    const std::string staging="_Foundry/"+aid+"/";
    if(path.compare(0,staging.size(),staging)==0)return true;
    if(root=="cache")return path.compare(0,3,"TU_")==0&&path.find('/')==std::string::npos;
    return path.size()>9&&path[8]=='/'&&FoundryHex(path.substr(0,8),8);
}
static bool Recover(std::string *error)
{
    ContentWriteLock lock;if(!lock.Ok()){*error="Could not lock content storage";return false;}
    cJSON *o=ReadJson(TXN);if(!o){if(Exists(TXN)){*error="Recovery journal is invalid; preserve it and inspect storage before retrying";return false;}return true;}
    Work w;w.games=Text(o,"games");w.content=Text(o,"content");std::string aid=Text(o,"artifact");
    const cJSON *a=cJSON_GetObjectItemCaseSensitive(o,"moves");bool ok=FoundryHex(aid,32)&&RootValid(w.games)&&RootValid(w.content)&&cJSON_IsArray(a)&&cJSON_GetArraySize(a)<1000;
    bool committed=cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(o,"committed"));
    for(int i=cJSON_GetArraySize(a)-1;ok&&!committed&&i>=0;--i)
    {
        const cJSON *m=cJSON_GetArrayItem(a,i);std::string kind=Text(m,"root"),root=Root(w,kind),from=Text(m,"from"),to=Text(m,"to");
        ok=!root.empty()&&JournalPath(from,kind,aid)&&JournalPath(to,kind,aid);
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
    // Avatar ownership changes canonical filenames after manifest validation.
    // Reject collisions before creating a journal or replacing installed files.
    std::set<std::string> destinations;
    for(size_t i=0;i<m.components.size();++i)for(size_t j=0;j<m.components[i].files.size();++j){
        const FoundryComponent &c=m.components[i];const FoundryFile &f=c.files[j];
        std::string key=Root(w,c.kind=="god"?"games":f.root)+"\\"+Native(f.path);
        std::transform(key.begin(),key.end(),key.begin(),::tolower);
        if(!destinations.insert(key).second){*error="Selected packages have conflicting installation paths; nothing replaced";return false;}
    }
    std::vector<Move> moves;
    for(int pass=0;pass<2;++pass) for(size_t i=0;i<m.components.size();++i)
    {
        const FoundryComponent &c=m.components[i];std::string root=c.kind=="god"?"games":c.files[0].root=="cache"?"cache":"content";
        if((c.kind=="god")!=(pass==0))continue;
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
                    std::string p=dir+"\\"+fd.cFileName;FILE *f=fopen(p.c_str(),"rb");unsigned char head[0x368];
                    bool match=f&&fread(head,1,sizeof(head),f)==sizeof(head)&&!memcmp(head,"LIVE",4)&&BE32(head+0x344)==0x7000&&Hex32(BE32(head+0x360))==c.titleId&&Hex32(BE32(head+0x354))==c.mediaId;
                    if(match&&c.discNumber)match=head[0x366]==c.discNumber;
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
            AddMove(&moves,root,c.files[0].installSource.empty()?staging+"/files/"+rel:c.files[0].installSource,rel);
        }
    }
    for(size_t i=0;i<moves.size();++i)
        if(Root(w,moves[i].root).size()+1+std::max(moves[i].from.size(),moves[i].to.size())>240){*error="Choose a shorter destination path for a recoverable Xbox installation";return false;}
    if(!SaveJournal(w,m.id,moves,false)){*error="Could not save the installation recovery journal";return false;}
    for(size_t i=0;i<moves.size();++i)
    {
        const Move &move=moves[i];std::string root=Root(w,move.root),from=root+"\\"+Native(move.from),to=root+"\\"+Native(move.to);
        if(!Parents(to)||Exists(to)||!MoveFileA(from.c_str(),to.c_str())){*error="Install commit failed; restoring previous packages";std::string recover;Recover(&recover);return false;}
    }
    if(!SaveJournal(w,m.id,moves,true)){*error="Could not finish the installation journal";return false;}
    return Recover(error);
}
static std::string Volume(std::string root)
{
    root=root.substr(0,root.find('\\'));
    for(size_t i=0;i<root.size();++i)if(root[i]>='A'&&root[i]<='Z')root[i]+='a'-'A';
    return root;
}
static bool CheckRequiredSpace(const Work &w,const FoundryManifest &m,std::string *error)
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
            required[Volume(root)]+=f.bytes-available;
            if(c.kind=="avatar"&&c.avatarUnlock)required[Volume(root)]+=f.bytes;
        }
    }
    for(std::map<std::string,unsigned long long>::const_iterator it=required.begin();it!=required.end();++it)
        if(!FreeSpace(it->first,it->second)){*error="Not enough free space on the selected destination for staging and recovery";return false;}
    return true;
}
static bool Prepared(const Work &w,const FoundryManifest &m,std::string *error)
{
    FoundryManifest installing=m;
    if(!CheckRequiredSpace(w,m,error))return false;
    unsigned files=0,index=0;unsigned long long completed=0;
    for(size_t i=0;i<m.components.size();++i)files+=(unsigned)m.components[i].files.size();
    for(size_t i=0;i<m.components.size();++i)
    {
        const FoundryComponent &c=m.components[i];std::string root=Root(w,c.kind=="god"?"games":c.files[0].root=="cache"?"cache":"content");
        for(size_t j=0;j<c.files.size();++j)
        {
            if(Cancelled()){*error="Cancelled; checked files are kept for resume";return false;}
            const FoundryFile &f=c.files[j];std::string label=f.path.substr(f.path.find_last_of('/')+1);
            if(c.kind=="god"&&c.discNumber)label="Disc "+Decimal(c.discNumber)+" "+label;
            BeginTransferFile(++index,files,completed,m.totalBytes,label);
            if(!Fetch(std::string(API)+"/artifacts/"+m.id+"/files/"+f.id,Stage(root,m.id)+"\\files\\"+Native(f.path),f.bytes,f.sha256,error))return false;
            g_transferBase=0;DownloadProgress(f.bytes,f.bytes,0,0);completed+=f.bytes;
        }
        const FoundryFile *package=&c.files[0];for(size_t j=0;j<c.files.size();++j)if(c.files[j].role=="header")package=&c.files[j];
        Phase("Checking package metadata",m.totalBytes?(float)((double)completed/m.totalBytes):-1,completed,m.totalBytes);if(!CheckPackage(Stage(root,m.id)+"\\files\\"+Native(package->path),c,error))return false;
        if(!PatchAvatarForInstall(w,m.id,&installing.components[i],error))return false;
    }
    // Validate TUs after every selected base game has arrived, independent of
    // manifest component ordering.
    for(size_t i=0;i<m.components.size();++i)
        if(m.components[i].kind=="title_update"&&!MatchingInstalledGame(w,m,m.components[i])){*error="Title update does not match the installed game's actual Media ID and base version";return false;}
    if(Cancelled()){*error="Cancelled before installation; checked files are kept";return false;}
    Phase("Installing; recovery journal saved",1,completed,m.totalBytes);return Commit(w,installing,error);
}
class StoredImage : public GodSource
{
    ZipPieces *pieces;unsigned long long start,length;
public:
    StoredImage(ZipPieces *p,unsigned long long s,unsigned long long n):pieces(p),start(s),length(n){}
    bool ReadAt(unsigned long long o,void *b,unsigned long n){return o<=length&&n<=length-o&&pieces->Read(start+o,b,n);}
    unsigned long long Size(){return length;}
};
static bool TimedProgress(const char *phase,unsigned long long done,unsigned long long total,DWORD started)
{
    Phase(phase,total?(float)((double)done/total):-1,done,total);
    DWORD elapsed=GetTickCount()-started;
    unsigned long long speed=elapsed>=1000?done*1000/elapsed:0,remaining=total>done?total-done:0;
    EnterCriticalSection(&g_lock);g_snap.bytesPerSec=speed;g_snap.secondsLeft=speed?remaining/speed+(remaining%speed!=0):0;LeaveCriticalSection(&g_lock);
    return !Cancelled();
}
static bool ConvertProgress(unsigned long long done,unsigned long long total,void *)
{return TimedProgress("Converting on Xbox",done,total,g_conversionStarted);}
static bool ConsoleConvert(const Work &w,const FoundryManifest &m,std::string *error)
{
    const FoundrySource &s=m.source;std::string dir=Stage(w.games,m.id)+"\\source";
    if(Exists(dir+"\\corrupt.flag"))RemoveTree(dir);
    unsigned long long missing=s.bytes;
    for(unsigned long long at=0;at<s.bytes;at+=PIECE)
    {
        char name[32];_snprintf(name,sizeof(name),"\\P%05lu.bin",(unsigned long)(at/PIECE));
        unsigned long long size=std::min(PIECE,s.bytes-at),have=Length(dir+name);
        if(have==size)missing-=size;
        else {have=Length(dir+name+".partial");if(have<=size)missing-=have;}
    }
    if(!Dirs(dir)||!FreeSpace(w.games,missing)){*error="Not enough space for the remaining source download";return false;}
    ZipPieces pieces(dir.c_str(),PIECE);Device device=DeviceCopy();
    br_sha256_context sha;br_sha256_init(&sha);std::vector<unsigned char> buffer(256*1024);
    for(unsigned long long at=0;at<s.bytes;at+=PIECE)
    {
        if(Cancelled()){*error="Cancelled; source pieces are kept";return false;}
        unsigned long long size=std::min(PIECE,s.bytes-at);char path[512];pieces.PiecePath((unsigned long)(at/PIECE),path,sizeof(path));
        char piece[32];_snprintf(piece,sizeof(piece),"Archive part %lu",(unsigned long)(at/PIECE)+1);piece[sizeof(piece)-1]=0;
        BeginTransferFile((unsigned)(at/PIECE)+1,(unsigned)((s.bytes+PIECE-1)/PIECE),at,s.bytes,piece);
        if(Length(path)!=size)
        {
            std::string partial=std::string(path)+".partial";
            if(!RangeDownload(std::string(API)+"/source-artifacts/"+m.id+"/file",partial,s.bytes,at,size,s.sha256,error))return false;
            DeleteFileA(path);if(!MoveFileA(partial.c_str(),path)){*error="Could not save source piece";return false;}
        }
        g_transferBase=0;DownloadProgress(size,size,0,0);
        for(unsigned long long n=0;n<size;n+=buffer.size())
        {unsigned long count=(unsigned long)std::min((unsigned long long)buffer.size(),size-n);if(!pieces.Read(at+n,&buffer[0],count)){*error="Source piece read failed";return false;}br_sha256_update(&sha,&buffer[0],count);}
    }
    unsigned char sum[32];char hex[65];br_sha256_out(&sha,sum);for(int i=0;i<32;++i)sprintf(hex+i*2,"%02x",sum[i]);hex[64]=0;
    if(s.sha256!=hex){FILE *flag=fopen((dir+"\\corrupt.flag").c_str(),"wb");if(flag)fclose(flag);*error="Source checksum failed; retry will fetch clean source pieces";return false;}
    Phase("Checking ISO and building conversion index");
    DWORD indexStarted=GetTickCount();
    InflateIndexer index(s.unpacked);unsigned long crc=crc32(0L,Z_NULL,0);
    for(unsigned long long at=0;at<s.packed;at+=buffer.size())
    {
        if(Cancelled()){*error="Cancelled; source archive is kept";return false;}
        unsigned long n=(unsigned long)std::min((unsigned long long)buffer.size(),s.packed-at);
        if(!pieces.Read(s.offset+at,&buffer[0],n)){*error="ISO read failed";return false;}
        if(s.method==8){if(!index.Ok()||!index.Feed(&buffer[0],n)){*error="Invalid compressed ISO";return false;}}
        else crc=crc32(crc,&buffer[0],n);
        TimedProgress("Checking ISO and building conversion index",at+n,s.packed,indexStarted);
    }
    if((s.method==8&&(!index.Finished()||index.Crc()!=s.crc||index.Out()!=s.unpacked))||(s.method==0&&crc!=s.crc)){*error="ISO CRC or length check failed";return false;}
    int count=0;InflatePoint *points=s.method==8?index.TakePoints(&count):NULL;
    InflateSource inflated(&pieces,s.offset,s.packed,s.unpacked,points,count);StoredImage stored(&pieces,s.offset,s.unpacked);
    GodSource *image=s.method==8?(GodSource*)&inflated:(GodSource*)&stored;GodImageInfo info;
    GodResult result=GodInspect(image,&info);
    if(result!=GOD_OK||info.title.contentType!=GOD_CONTENT_GAMES_ON_DEMAND||(!s.titleId.empty()&&Hex32(info.title.titleId)!=s.titleId))
    {free(points);*error="ISO inspection failed or the disc identity differs";return false;}
    std::string output=Stage(w.games,m.id)+"\\files";
    RemoveTree(output); // Only this artifact's unpublished conversion, after journal recovery.
    if(!FreeSpace(w.games,info.outputSize)){free(points);*error="Source is saved. Free space for the converted game, then resume";return false;}
    Dirs(output);char package[600];
    g_conversionStarted=GetTickCount();
    {ReadAheadSource ahead(image,1024*1024,8);result=GodConvert(&ahead,info,output.c_str(),s.name.c_str(),NULL,0,ConvertProgress,NULL,package,sizeof(package));}
    free(points);if(result!=GOD_OK){*error=GodResultText(result);return false;}
    FoundryManifest generated;generated.id=m.id;FoundryComponent c;c.kind="god";c.titleId=Hex32(info.title.titleId);c.mediaId=Hex32(info.title.mediaId);c.contentType="00007000";
    c.discNumber=info.title.discNumber;c.discCount=info.title.discCount;
    std::string relative=std::string(package).substr(output.size()+1);std::replace(relative.begin(),relative.end(),'\\','/');
    FoundryFile header;header.path=relative;header.root="content";header.role="header";c.files.push_back(header);generated.components.push_back(c);
    if(!CheckPackage(package,c,error))return false;
    Phase("Installing converted game");return Commit(w,generated,error);
}
static bool SaveWork(const Work &w)
{
    cJSON *o=cJSON_CreateObject();cJSON_AddStringToObject(o,"request",w.request.c_str());cJSON_AddStringToObject(o,"name",w.name.c_str());
    cJSON_AddStringToObject(o,"games",w.games.c_str());cJSON_AddStringToObject(o,"content",w.content.c_str());
    cJSON_AddStringToObject(o,"body",w.body.c_str());cJSON_AddStringToObject(o,"key",w.key.c_str());
    cJSON_AddStringToObject(o,"origin",w.origin.c_str());cJSON_AddStringToObject(o,"device",w.device.c_str());cJSON_AddBoolToObject(o,"inbox",w.inbox);cJSON_AddBoolToObject(o,"addon",w.addon);cJSON_AddStringToObject(o,"art_title_id",Hex32(w.art).c_str());
    bool ok=WriteJson(JOB,o);cJSON_Delete(o);return ok;
}
static bool RememberWork(const Work &w,std::string *error)
{
    if(!SaveWork(w)){*error="Could not persist the resumable installation job";return false;}
    EnterCriticalSection(&g_lock);g_work=w;LeaveCriticalSection(&g_lock);return true;
}
static bool NewRequestKey(Work *w,std::string *error)
{
    EnterCriticalSection(&g_lock);++g_device.sequence;bool saved=SaveDevice();
    w->key=g_device.id+"_"+Decimal(g_device.sequence);w->origin=g_device.origin;w->device=g_device.id;LeaveCriticalSection(&g_lock);
    if(!saved){*error="Could not save the request counter";return false;}
    return RememberWork(*w,error);
}
static bool LoadWork(Work *w)
{
    cJSON *o=ReadJson(JOB,256*1024);if(!o)return false;
    w->request=Text(o,"request");w->name=Text(o,"name");w->games=Text(o,"games");w->content=Text(o,"content");
    w->body=Text(o,"body");w->key=Text(o,"key");w->origin=Text(o,"origin");w->device=Text(o,"device");w->inbox=cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(o,"inbox"));w->addon=cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(o,"addon"));std::string art=Text(o,"art_title_id");w->art=FoundryHex(art,8)?strtoul(art.c_str(),NULL,16):0;cJSON_Delete(o);
    bool pending=w->request.empty()&&!w->body.empty()&&w->body.size()<=65536&&!w->key.empty();
    if(!w->key.empty()&&(w->key.size()<16||w->key.size()>96||w->key.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")!=std::string::npos))return false;
    return (pending||FoundryHex(w->request,32))&&RootValid(w->games)&&RootValid(w->content)
        && (w->origin.empty()||FoundryOrigin(w->origin)) && (w->device.empty()||FoundryHex(w->device,32));
}
static bool CreatePendingRequest(Work *w,std::string *error)
{
    Device d=DeviceCopy();
    if(d.token.empty()||!FoundryHex(d.id,32)){*error="Pair this console before resuming";return false;}
    if(!w->origin.empty()&&w->origin!=d.origin){*error="Select the original Foundry server before resuming this job";return false;}
    if(!w->device.empty()&&w->device!=d.id)
    {
        if(w->inbox||w->body.empty()){*error="This saved job belongs to the original pairing; discard it before choosing a new request";return false;}
        w->request.clear();w->key.clear();
    }
    if(w->origin.empty()||w->device.empty()){w->origin=d.origin;w->device=d.id;if(!RememberWork(*w,error))return false;}
    if(!w->request.empty())return true;
    if(w->body.empty()){*error="No saved selection is available; choose the game again";return false;}
    if(w->key.empty()&&!NewRequestKey(w,error))return false;
    cJSON *reply=NULL;
    if(!Call(std::string(API)+"/install-requests",w->body,true,&reply,error,false,w->key))return false;
    w->request=Text(reply,"id");cJSON_Delete(reply);
    if(!FoundryHex(w->request,32)){*error="Server returned an invalid request ID";return false;}
    return RememberWork(*w,error);
}
static void Finish(bool ok,const std::string &error)
{
    EnterCriticalSection(&g_lock);g_snap.state=QUEUE_FINISHED;g_snap.outcome=ok?QUEUE_OUTCOME_INSTALLED:Cancelled()?QUEUE_OUTCOME_CANCELLED:QUEUE_OUTCOME_FAILED;
    g_snap.fraction=ok?1:-1;g_snap.notify=true;g_delivered=false;
    _snprintf(g_snap.resultText,sizeof(g_snap.resultText),"%s",ok?"Installed":Cancelled()?"Paused":"Installation failed");
    _snprintf(g_snap.resultDetail,sizeof(g_snap.resultDetail),"%s",ok?"Ready in the library. Enable selected title updates in Aurora.":error.c_str());
    g_snap.resultText[sizeof(g_snap.resultText)-1]=0;g_snap.resultDetail[sizeof(g_snap.resultDetail)-1]=0;
    if(ok){FinishedWork done;done.work=g_work;done.snap=g_snap;done.snap.notify=false;g_queueIds[WorkKey(done.work)]=done.snap.id;
        for(size_t i=0;i<g_finished.size();)if(WorkKey(g_finished[i].work)==WorkKey(done.work))g_finished.erase(g_finished.begin()+i);else ++i;
        g_finished.insert(g_finished.begin(),done);if(g_finished.size()>32)g_finished.pop_back();
        std::vector<Work> queue;if(LoadInbox(&queue))SaveInbox(queue);}
    LeaveCriticalSection(&g_lock);
}
static void InitSnap(const Work &w)
{
    SnapWork(w,&g_snap);g_used=true;g_delivered=false;
}
static bool QueuePayload(const char *body,const std::string &name,const char *games,const char *content,std::string *error,unsigned long art=0,bool addon=false)
{
    EnterCriticalSection(&g_lock);bool active=g_used&&g_snap.state!=QUEUE_FINISHED;
    if(active||Exists(JOB)){*error=active?"A Foundry installation is already active":"Resume or discard the interrupted Foundry job first";LeaveCriticalSection(&g_lock);return false;}
    Work w;w.body=body?body:"";w.name=name;w.games=games;w.content=content;w.art=art;w.addon=addon;
    if(w.body.empty()||w.body.size()>65536||!RootValid(w.games)||!RootValid(w.content)){*error="Invalid install selection or destination";LeaveCriticalSection(&g_lock);return false;}
    Device d=g_device;if(d.token.empty()||!FoundryHex(d.id,32)){*error="Pair this console first";LeaveCriticalSection(&g_lock);return false;}
    if(!NewRequestKey(&w,error)){LeaveCriticalSection(&g_lock);return false;}
    InterlockedExchange(&g_storePrefetchCancel,1);InitSnap(w);InterlockedExchange(&g_cancel,0);g_pending=true;
    LeaveCriticalSection(&g_lock);SetEvent(g_wake);return true;
}

// The inbox list is separate from FoundryJob.json. Persist the active job before
// removing its list entry, so power loss cannot lose a selected download.
static bool SaveInbox(const std::vector<Work> &queue,const std::string &completed)
{
    cJSON *previous=ReadJson(INBOX,3*1024*1024);const cJSON *oldDone=cJSON_GetObjectItemCaseSensitive(previous,"completed");
    cJSON *done=cJSON_IsArray(oldDone)?cJSON_Duplicate(oldDone,1):cJSON_CreateArray();cJSON_Delete(previous);
    if(!completed.empty()){bool found=false;for(int i=0;i<cJSON_GetArraySize(done);++i){const cJSON *v=cJSON_GetArrayItem(done,i);found|=cJSON_IsString(v)&&completed==v->valuestring;}if(!found)cJSON_AddItemToArray(done,cJSON_CreateString(completed.c_str()));}
    while(cJSON_GetArraySize(done)>256)cJSON_DeleteItemFromArray(done,0);
    cJSON *o=cJSON_CreateObject(),*rows=cJSON_AddArrayToObject(o,"items");
    for(size_t i=0;i<queue.size();++i){const Work &w=queue[i];cJSON *v=cJSON_CreateObject();
        cJSON_AddStringToObject(v,"request",w.request.c_str());cJSON_AddStringToObject(v,"name",w.name.c_str());
        cJSON_AddStringToObject(v,"games",w.games.c_str());cJSON_AddStringToObject(v,"content",w.content.c_str());
        cJSON_AddStringToObject(v,"body",w.body.c_str());cJSON_AddStringToObject(v,"origin",w.origin.c_str());cJSON_AddStringToObject(v,"device",w.device.c_str());cJSON_AddBoolToObject(v,"addon",w.addon);cJSON_AddStringToObject(v,"art_title_id",Hex32(w.art).c_str());cJSON_AddItemToArray(rows,v);}
    cJSON *history=cJSON_AddArrayToObject(o,"history");
    for(size_t i=0;i<g_finished.size();++i){const FinishedWork &f=g_finished[i];cJSON *v=cJSON_CreateObject();
        cJSON_AddStringToObject(v,"request",f.work.request.c_str());cJSON_AddStringToObject(v,"name",f.work.name.c_str());cJSON_AddStringToObject(v,"origin",f.work.origin.c_str());cJSON_AddStringToObject(v,"device",f.work.device.c_str());
        cJSON_AddStringToObject(v,"art_title_id",Hex32(f.snap.titleId).c_str());cJSON_AddBoolToObject(v,"addon",f.work.addon);cJSON_AddItemToArray(history,v);}
    cJSON_AddItemToObject(o,"completed",done);bool ok=WriteJson(INBOX,o);cJSON_Delete(o);
    if(ok){g_waiting=queue;
        std::set<std::string> live;live.insert(WorkKey(g_work));for(size_t i=0;i<queue.size();++i)live.insert(WorkKey(queue[i]));for(size_t i=0;i<g_finished.size();++i)live.insert(WorkKey(g_finished[i].work));
        for(std::map<std::string,int>::iterator i=g_queueIds.begin();i!=g_queueIds.end();)if(!live.count(i->first)){std::map<std::string,int>::iterator old=i++;g_queueIds.erase(old);}else ++i;
    }return ok;
}
static bool LoadInbox(std::vector<Work> *queue)
{
    queue->clear();if(!Exists(INBOX)&&!Exists((std::string(INBOX)+".old"))){g_waiting.clear();g_inboxLoaded=true;return true;}
    cJSON *o=ReadJson(INBOX,3*1024*1024);if(!o)return false;
    const cJSON *rows=cJSON_GetObjectItemCaseSensitive(o,"items");bool ok=cJSON_IsArray(rows)&&cJSON_GetArraySize(rows)<=32;
    for(int i=0;ok&&i<cJSON_GetArraySize(rows);++i){const cJSON *v=cJSON_GetArrayItem(rows,i);Work w;w.inbox=true;
        w.request=Text(v,"request");w.name=Text(v,"name");w.games=Text(v,"games");w.content=Text(v,"content");w.body=Text(v,"body");w.origin=Text(v,"origin");w.device=Text(v,"device");w.addon=cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(v,"addon"));std::string art=Text(v,"art_title_id");w.art=FoundryHex(art,8)?strtoul(art.c_str(),NULL,16):0;
        ok=FoundryHex(w.request,32)&&FoundryHex(w.device,32)&&FoundryOrigin(w.origin)&&RootValid(w.games)&&RootValid(w.content)&&w.name.size()<=512&&w.body.size()<=65536;
        for(size_t j=0;ok&&j<queue->size();++j)ok=(*queue)[j].request!=w.request;
        if(ok)queue->push_back(w);}
    if(ok&&!g_inboxLoaded){const cJSON *history=cJSON_GetObjectItemCaseSensitive(o,"history");
        for(int i=0;cJSON_IsArray(history)&&i<cJSON_GetArraySize(history)&&i<32;++i){const cJSON *v=cJSON_GetArrayItem(history,i);FinishedWork f;
            f.work.request=Text(v,"request");f.work.name=Text(v,"name");f.work.origin=Text(v,"origin");f.work.device=Text(v,"device");f.work.addon=cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(v,"addon"));std::string art=Text(v,"art_title_id");f.work.art=FoundryHex(art,8)?strtoul(art.c_str(),NULL,16):0;
            if(!FoundryHex(f.work.request,32)||!FoundryHex(f.work.device,32)||!FoundryOrigin(f.work.origin)||f.work.name.empty()||f.work.name.size()>512)continue;
            SnapWork(f.work,&f.snap);f.snap.state=QUEUE_FINISHED;f.snap.outcome=QUEUE_OUTCOME_INSTALLED;f.snap.fraction=1;strcpy(f.snap.resultText,"Installed");g_finished.push_back(f);}
        g_inboxLoaded=true;}
    cJSON_Delete(o);if(!ok)queue->clear();else g_waiting=*queue;return ok;
}
bool FoundryPreparedCompleted(const std::string &id)
{
    EnterCriticalSection(&g_lock);cJSON *o=ReadJson(INBOX,3*1024*1024);const cJSON *a=cJSON_GetObjectItemCaseSensitive(o,"completed");bool found=false;
    for(int i=0;cJSON_IsArray(a)&&i<cJSON_GetArraySize(a)&&i<256;++i){const cJSON *v=cJSON_GetArrayItem(a,i);found|=cJSON_IsString(v)&&id==v->valuestring;}
    cJSON_Delete(o);LeaveCriticalSection(&g_lock);return found;
}
static bool QueuePrepared(const std::vector<Work> &incoming,std::string *error)
{
    Device d=DeviceCopy();EnterCriticalSection(&g_lock);std::vector<Work> queue;bool ok=LoadInbox(&queue);
    if(!ok)*error="Saved prepared list needs review; clear it in Prepared for Xbox";
    for(size_t i=0;ok&&i<incoming.size();++i){const Work &w=incoming[i];
        if(!FoundryHex(w.request,32)||w.origin!=d.origin||w.device!=d.id||!RootValid(w.games)||!RootValid(w.content)||w.body.size()>65536){*error="Prepared request or destination is invalid";ok=false;break;}
        bool duplicate=FoundryPreparedCompleted(w.request)||(Exists(JOB)&&g_work.request==w.request);
        for(size_t j=0;j<queue.size();++j){if(queue[j].origin!=d.origin||queue[j].device!=d.id){*error="The saved list belongs to another server/pairing; clear it first";ok=false;break;}if(queue[j].request==w.request){queue[j].addon=w.addon;queue[j].art=w.art;}duplicate|=queue[j].request==w.request;}
        if(ok&&!duplicate){if(queue.size()>=32){*error="The saved prepared list is full (32 items)";ok=false;}else queue.push_back(w);}}
    if(ok)std::stable_sort(queue.begin(),queue.end(),GameBeforeAddon);
    if(ok&&!SaveInbox(queue)){*error="Could not save the prepared download list";ok=false;}
    LeaveCriticalSection(&g_lock);if(ok)SetEvent(g_wake);return ok;
}
static bool StartNextPrepared(std::string *error)
{
    EnterCriticalSection(&g_lock);
    if(Exists(JOB)||(g_used&&(g_snap.state!=QUEUE_FINISHED||!g_delivered))){LeaveCriticalSection(&g_lock);return false;}
    std::vector<Work> queue;bool ok=LoadInbox(&queue);if(!ok||queue.empty()){LeaveCriticalSection(&g_lock);return false;}
    Work w=queue[0];Device d=g_device;
    if(w.origin!=d.origin||w.device!=d.id||d.token.empty()){*error="Prepared list belongs to another server/pairing";LeaveCriticalSection(&g_lock);return false;}
    ok=RememberWork(w,error);
    if(ok){queue.erase(queue.begin());SaveInbox(queue); /* JOB is already durable; completion also removes any duplicate entry. */}
    if(ok){InitSnap(w);g_pending=true;InterlockedExchange(&g_cancel,0);}
    LeaveCriticalSection(&g_lock);return ok;
}
static bool ReadPrepared(const cJSON *reply,const char *games,const char *content,std::vector<Work> *ready,const Device *owner=NULL)
{
    ready->clear();Device d=owner?*owner:DeviceCopy();const cJSON *rows=cJSON_GetObjectItemCaseSensitive(reply,"items");
    if(!cJSON_IsArray(rows)||cJSON_GetArraySize(rows)>32)return false;
    for(int i=0;i<cJSON_GetArraySize(rows);++i){const cJSON *v=cJSON_GetArrayItem(rows,i);if(Text(v,"state")!="ready"||FoundryPreparedCompleted(Text(v,"id")))continue;
        if(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(v,"wait_for_base")))continue;
        if(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(v,"contains_avatar"))&&cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(v,"avatar_unlock"))&&!FoundryCanUnlockAvatars())continue;
        Work w;w.inbox=true;w.request=Text(v,"id");w.name=Text(v,"name");w.games=games;w.content=content;w.origin=d.origin;w.device=d.id;w.addon=Text(v,"content_kind")=="addon";std::string art=Text(v,"art_title_id");w.art=FoundryHex(art,8)?strtoul(art.c_str(),NULL,16):0;
        const cJSON *selection=cJSON_GetObjectItemCaseSensitive(v,"selection");if(!FoundryHex(w.request,32)||w.name.empty()||w.name.size()>512||!cJSON_IsObject(selection))return false;
        char *raw=cJSON_PrintUnformatted(selection);if(!raw)return false;w.body=raw;cJSON_free(raw);if(w.body.size()>65536)return false;ready->push_back(w);}
    std::stable_sort(ready->begin(),ready->end(),GameBeforeAddon);return true;
}

static void FilterPrepared(const cJSON *reply,const std::string &only,std::vector<Work> *ready)
{
    if(!only.empty()){for(size_t i=0;i<ready->size();){bool member=(*ready)[i].request==only;const cJSON *rows=cJSON_GetObjectItemCaseSensitive(reply,"items");
        for(int j=0;!member&&j<cJSON_GetArraySize(rows);++j){const cJSON *row=cJSON_GetArrayItem(rows,j);member=Text(row,"id")==(*ready)[i].request&&Text(row,"group_id")==only;}
        if(!member)ready->erase(ready->begin()+i);else ++i;}}
}

static bool RefreshQueuedMetadata(const cJSON *reply,std::string *error)
{
    const cJSON *rows=cJSON_GetObjectItemCaseSensitive(reply,"items");if(!cJSON_IsArray(rows)||cJSON_GetArraySize(rows)>32)return false;
    Device device=DeviceCopy();EnterCriticalSection(&g_lock);std::vector<Work> queue;bool ok=LoadInbox(&queue);
    for(size_t i=0;ok&&i<queue.size();++i)if(queue[i].origin==device.origin&&queue[i].device==device.id){
        for(int j=0;j<cJSON_GetArraySize(rows);++j){const cJSON *row=cJSON_GetArrayItem(rows,j);if(Text(row,"id")!=queue[i].request)continue;
            std::string kind=Text(row,"content_kind"),art=Text(row,"art_title_id");if(kind=="game"||kind=="addon")queue[i].addon=kind=="addon";
            if(FoundryHex(art,8))queue[i].art=strtoul(art.c_str(),NULL,16);break;}}
    if(ok){std::stable_sort(queue.begin(),queue.end(),GameBeforeAddon);ok=SaveInbox(queue);}if(!ok)*error="Could not refresh the saved queue";
    LeaveCriticalSection(&g_lock);return ok;
}

static DWORD WINAPI Worker(LPVOID)
{
    DWORD lastInbox=GetTickCount()-60000;bool refreshedQueue=false;
    while(!InterlockedCompareExchange(&g_stop,0,0))
    {
        WaitForSingleObject(g_wake,1000);if(g_stop)break;
        std::string inboxError;
        // Upgrade metadata on older saved lists once, keeping their request IDs
        // and partial downloads. Active/interrupted jobs still resume first.
        if(!refreshedQueue&&!Exists(JOB)){
            EnterCriticalSection(&g_lock);bool waiting=!g_waiting.empty();LeaveCriticalSection(&g_lock);
            if(waiting){cJSON *rows=NULL;if(Call(std::string(API)+"/inbox","",true,&rows,&inboxError,false,"",NULL,1))RefreshQueuedMetadata(rows,&inboxError);cJSON_Delete(rows);}refreshedQueue=true;}
        StartNextPrepared(&inboxError);
        Device device=DeviceCopy();
        EnterCriticalSection(&g_lock);bool idle=!g_pending&&(!g_used||g_snap.state==QUEUE_FINISHED);LeaveCriticalSection(&g_lock);
        if(idle&&!Exists(JOB)&&!device.token.empty()&&GetTickCount()-lastInbox>=60000)
        {
            lastInbox=GetTickCount();cJSON *reply=NULL;
            if(Call(std::string(API)+"/inbox","",true,&reply,&inboxError,false,"",NULL,1,&device))
            {
                // Retry missing success reports from durable local receipts. A
                // lost report never causes an automatic duplicate installation.
                const cJSON *rows=cJSON_GetObjectItemCaseSensitive(reply,"items");unsigned retried=0;
                for(int i=0;cJSON_IsArray(rows)&&i<cJSON_GetArraySize(rows)&&i<32&&retried<8&&!Cancelled();++i){std::string id=Text(cJSON_GetArrayItem(rows,i),"id");
                    if(FoundryHex(id,32)&&FoundryPreparedCompleted(id)){++retried;cJSON *ack=NULL;std::string ignored,body="{\"event_id\":\""+id+"_installed\",\"outcome\":\"installed\"}";
                        Call(std::string(API)+"/install-requests/"+id+"/events",body,true,&ack,&ignored,false,"",NULL,1,&device);cJSON_Delete(ack);}}
                QueueJobSnapshot other[MAX_QUEUE_JOBS];bool busy=false;int count=SnapshotDownloadQueue(other,MAX_QUEUE_JOBS);
                for(int j=0;j<count;++j)busy|=other[j].state!=QUEUE_FINISHED;
                count=SnapshotGameJobs(other,MAX_QUEUE_JOBS);for(int j=0;j<count;++j)busy|=other[j].state!=QUEUE_FINISHED;
                count=SnapshotDiscJobs(other,MAX_QUEUE_JOBS);for(int j=0;j<count;++j)busy|=other[j].state!=QUEUE_FINISHED;
                EnterCriticalSection(&g_lock);std::string games=g_defaultGames,content=g_defaultContent;bool automatic=g_device.autoInbox;LeaveCriticalSection(&g_lock);
                std::vector<Work> ready;if(device.autoInbox&&automatic&&!busy&&ReadPrepared(reply,games.c_str(),content.c_str(),&ready,&device)&&!ready.empty())QueuePrepared(ready,&inboxError);
            }
            cJSON_Delete(reply);StartNextPrepared(&inboxError);
        }
        EnterCriticalSection(&g_lock);bool pending=g_pending;g_pending=false;Work w=g_work;if(pending)g_snap.state=QUEUE_ACTIVE;LeaveCriticalSection(&g_lock);
        if(!pending)continue;g_transferJobDone=g_transferJobTotal=0;g_transferFiles=g_transferFile=0;
        std::string error;bool ok=Recover(&error)&&CreatePendingRequest(&w,&error);std::string aid;
        while(ok&&!Cancelled())
        {
            Phase("Waiting for server preparation");cJSON *reply=NULL;
            if(!Call(std::string(API)+"/install-requests/"+w.request,"",true,&reply,&error)){ok=false;break;}
            std::string state=Text(reply,"state"),message=Text(reply,"message");aid=Text(reply,"artifact_id");
            if(w.body.empty())
            {
                const cJSON *selection=cJSON_GetObjectItemCaseSensitive(reply,"selection");
                if(cJSON_IsObject(selection)){char *raw=cJSON_PrintUnformatted(selection);if(raw){w.body=raw;cJSON_free(raw);ok=RememberWork(w,&error);}}
            }
            cJSON_Delete(reply);if(!ok)break;
            if(state=="expired"&&!w.inbox&&!w.body.empty())
            {
                w.request.clear();w.key.clear();
                ok=NewRequestKey(&w,&error)&&CreatePendingRequest(&w,&error);
                continue;
            }
            if(state=="ready"){
                if(w.inbox){cJSON *ack=NULL;std::string ignored;Call(std::string(API)+"/inbox/"+w.request+"/accept","{}",true,&ack,&ignored,false,"",NULL,1);cJSON_Delete(ack);}
                break;}
            if(state=="failed"||state=="expired"||state=="cancelled"){
                // An expired/deleted immutable snapshot cannot resume. Recovery
                // has already completed; release only its saved request so a
                // fresh website preparation can proceed without blocking others.
                if(w.inbox&&(state=="expired"||state=="cancelled")){DeleteFileA(JOB);DeleteFileA((std::string(JOB)+".old").c_str());}
                error=message;ok=false;break;}
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
                if(ok){for(size_t i=0;i<manifest.components.size();++i)if(manifest.components[i].kind=="god"||manifest.components[i].kind=="xbla"){
                    EnterCriticalSection(&g_lock);if(!g_snap.titleId)g_snap.titleId=strtoul(manifest.components[i].titleId.c_str(),NULL,16);LeaveCriticalSection(&g_lock);break;}
                    ok=manifest.conversion=="server"?Prepared(w,manifest,&error):ConsoleConvert(w,manifest,&error);}
            }
        }
        if(ok)
        {
            bool receipt=true;
            if(w.inbox){EnterCriticalSection(&g_lock);std::vector<Work> queue;receipt=LoadInbox(&queue);
                for(size_t i=0;receipt&&i<queue.size();)if(queue[i].request==w.request)queue.erase(queue.begin()+i);else ++i;
                if(receipt)receipt=SaveInbox(queue,w.request);LeaveCriticalSection(&g_lock);}
            if(receipt){DeleteFileA(JOB);DeleteFileA((std::string(JOB)+".old").c_str());}
            else dprintf("[foundry] completed install receipt could not be saved; job retained for recovery\n");
            RemoveTree(Stage(w.games,aid));if(w.content!=w.games)RemoveTree(Stage(w.content,aid));RemoveTree(Stage(Root(w,"cache"),aid));
        }
        // Reports are best effort and never turn a successful local commit into
        // a failed installation. The local journal remains the source of truth.
        cJSON *event=cJSON_CreateObject();cJSON_AddStringToObject(event,"event_id",(w.request+"_"+(ok?"installed":Cancelled()?"cancelled":"failed")).c_str());
        cJSON_AddStringToObject(event,"outcome",ok?"installed":Cancelled()?"cancelled":"failed");char *body=cJSON_PrintUnformatted(event);cJSON *ignored=NULL;std::string ignoredError;
        if(FoundryHex(w.request,32))Call(std::string(API)+"/install-requests/"+w.request+"/events",body,true,&ignored,&ignoredError,false,"",NULL,1);cJSON_free(body);cJSON_Delete(event);cJSON_Delete(ignored);
        Finish(ok,error);
    }
    return 0;
}

bool StartFoundry(const char *gamesRoot,const char *contentRoot)
{
    if(g_started)return true;g_defaultGames=gamesRoot;g_defaultContent=contentRoot;InitializeCriticalSection(&g_lock);LoadDevice();InterlockedExchange(&g_stop,0);InterlockedExchange(&g_cancel,0);
    std::string recoveryError;if(!Recover(&recoveryError))dprintf("[foundry] recovery required: %s\n",recoveryError.c_str());
    g_wake=CreateEvent(NULL,FALSE,FALSE,NULL);if(!g_wake){DeleteCriticalSection(&g_lock);return false;}
    g_thread=CreateThread(NULL,512*1024,Worker,NULL,CREATE_SUSPENDED,NULL);if(!g_thread){CloseHandle(g_wake);DeleteCriticalSection(&g_lock);return false;}
    g_started=true;std::vector<Work> inbox;LoadInbox(&inbox);
    Work w;if(LoadWork(&w)){
        if(w.inbox&&recoveryError.empty()&&FoundryPreparedCompleted(w.request)){DeleteFileA(JOB);DeleteFileA((std::string(JOB)+".old").c_str());}
        else{EnterCriticalSection(&g_lock);g_work=w;InitSnap(w);g_pending=true;LeaveCriticalSection(&g_lock);SetEvent(g_wake);}}
    ResumeThread(g_thread);return true;
}
void StopFoundry(DWORD timeoutMs)
{
    if(!g_started)return;InterlockedExchange(&g_stop,1);InterlockedExchange(&g_cancel,1);SetEvent(g_wake);
    InterlockedExchange(&g_storePrefetchCancel,1);DWORD started=GetTickCount();
    bool stopped=WaitForSingleObject(g_thread,timeoutMs)==WAIT_OBJECT_0;
    DWORD elapsed=GetTickCount()-started;DWORD left=elapsed<timeoutMs?timeoutMs-elapsed:0;
    bool prefetched=!g_storePrefetch||WaitForSingleObject(g_storePrefetch,left)==WAIT_OBJECT_0;
    if(stopped&&prefetched){if(g_storePrefetch){CloseHandle(g_storePrefetch);g_storePrefetch=NULL;}CloseHandle(g_thread);CloseHandle(g_wake);ClearFoundryStoreCache();
        HttpsSessionClose(g_storeSession);g_storeSession=NULL;HttpsSessionClose(g_storePrefetchSession);g_storePrefetchSession=NULL;g_storeSessionOwner.clear();g_storePrefetchOwner.clear();g_started=false;}
}
void SetFoundryInstallPaths(const char *games,const char *content)
{
    if(!g_started||!games||!content||!RootValid(games)||!RootValid(content))return;
    EnterCriticalSection(&g_lock);g_defaultGames=games;g_defaultContent=content;LeaveCriticalSection(&g_lock);
}
int SnapshotFoundryActiveJob(QueueJobSnapshot *out)
{if(!g_started)return 0;EnterCriticalSection(&g_lock);int n=g_used?1:0;if(n)*out=g_snap;LeaveCriticalSection(&g_lock);return n;}
int SnapshotFoundryJobs(QueueJobSnapshot *out,int maximum)
{
    if(!g_started||maximum<1)return 0;EnterCriticalSection(&g_lock);int n=0;
    if(g_used&&(g_snap.state!=QUEUE_FINISHED||g_snap.outcome!=QUEUE_OUTCOME_INSTALLED))out[n++]=g_snap;
    for(size_t i=0;i<g_waiting.size()&&n<maximum;++i){const Work &w=g_waiting[i];
        if(w.origin!=g_device.origin||w.device!=g_device.id||(g_used&&g_work.request==w.request))continue;
        SnapWork(w,&out[n++]);}
    if(g_used&&g_snap.state==QUEUE_FINISHED&&g_snap.outcome==QUEUE_OUTCOME_INSTALLED&&n<maximum)out[n++]=g_snap;
    for(size_t i=0;i<g_finished.size()&&n<maximum;++i){const FinishedWork &f=g_finished[i];
        if(f.work.origin==g_device.origin&&f.work.device==g_device.id&&(!g_used||f.snap.id!=g_snap.id))out[n++]=f.snap;}
    LeaveCriticalSection(&g_lock);return n;
}
bool TakeFinishedFoundryJob(QueueJobSnapshot *out)
{if(!g_started)return false;EnterCriticalSection(&g_lock);bool ready=g_used&&g_snap.state==QUEUE_FINISHED&&!g_delivered;if(ready){*out=g_snap;g_delivered=true;}LeaveCriticalSection(&g_lock);return ready;}
bool IsFoundryJobId(int id){return id>=JOB_ID&&id<4000000;}
void CancelFoundryJob(int id)
{
    EnterCriticalSection(&g_lock);
    if(g_used&&g_snap.id==id&&g_snap.state!=QUEUE_FINISHED)InterlockedExchange(&g_cancel,1);
    else{std::vector<Work> queue;if(LoadInbox(&queue)){for(size_t i=0;i<queue.size();)if(WorkId(queue[i])==id)queue.erase(queue.begin()+i);else ++i;SaveInbox(queue);}}
    LeaveCriticalSection(&g_lock);
}
void RemoveFoundryJob(int id)
{
    EnterCriticalSection(&g_lock);if(g_used&&g_snap.id==id&&g_snap.state==QUEUE_FINISHED)g_used=false;
    for(size_t i=0;i<g_finished.size();)if(g_finished[i].snap.id==id)g_finished.erase(g_finished.begin()+i);else ++i;
    std::vector<Work> queue;if(LoadInbox(&queue))SaveInbox(queue);LeaveCriticalSection(&g_lock);
}

static void PairUI()
{
    cJSON *reply=NULL;if(!UiCall(std::string(API)+"/pairings","{\"name\":\"Xbox 360\"}",false,&reply))return;
    std::string secret=Text(reply,"device_code"),code=Text(reply,"user_code"),url=Text(reply,"verification_uri");cJSON_Delete(reply);
    if(secret.size()<32||code.size()!=9){ShowMessageUI("Pairing failed","Invalid pairing response",NULL);return;}
    std::string detail=url+"  Code: "+code;
    if(!ShowConfirmUI("Pair Foundry","Open the website on your phone or computer and approve this code.",detail.c_str(),"I approved it"))return;
    cJSON *body=cJSON_CreateObject();cJSON_AddStringToObject(body,"device_code",secret.c_str());char *raw=cJSON_PrintUnformatted(body);
    DWORD started=GetTickCount();
    while(GetTickCount()-started<600000)
    {
        if(!UiCall(std::string(API)+"/pairings/token",raw,false,&reply))break;
        std::string token=Text(reply,"access_token"),id=Text(reply,"device_id");
        if(Text(reply,"status")=="approved"&&token.size()>=40&&token.size()<100&&FoundryHex(id,32))
        {
            EnterCriticalSection(&g_lock);g_device.token=token;g_device.id=id;g_device.sequence=0;bool saved=SaveDevice();LeaveCriticalSection(&g_lock);
            ShowMessageUI("Foundry",saved?"Console paired":"Could not save the device token",NULL);
            cJSON_Delete(reply);break;
        }
        cJSON_Delete(reply);
        DWORD wait=GetTickCount();bool cancel=false;
        while(GetTickCount()-wait<5000)
        {
            RenderProgressFrame("Pair Foundry","Waiting for website approval",detail.c_str(),-1,"Pairing");
            if(PollUiInput().pressed&XINPUT_GAMEPAD_B){cancel=true;break;}Sleep(16);
        }
        if(cancel)break;
    }
    cJSON_free(raw);cJSON_Delete(body);
}
static bool ChoosePackageLicenses(cJSON *body)
{
    const char *choices[]={"Unlock XBLA / DLC / Avatar items","Leave licenses unchanged"};
    const char *notes[]={"DLC/XBLA unlock on server; Avatars bind to signed-in compatible profiles on Xbox. GoD/TUs unchanged.","Keep supplied licenses, including content that is already unlocked."};
    int pick=ShowChoiceUI("Package licenses","Apply this choice to all selected packages",choices,notes,2,L"Choose");
    if(pick<0)return false;cJSON_AddBoolToObject(body,"unlock_packages",pick==0);return true;
}
static void QueueRelease(const std::string &release,const std::string &name,const char *games,const char *content,bool digital,const std::string &forTitle=std::string(),unsigned long art=0)
{
    QueueJobSnapshot snap;if(SnapshotFoundryActiveJob(&snap)&&snap.state!=QUEUE_FINISHED){ShowMessageUI("Foundry","One Foundry installation is already active","Open Queue to see progress.");return;}
    const char *modes[]={"Convert on server","Convert on Xbox"};const char *details[]={"Download prepared and checked GoD files","Download the ISO archive, then convert locally"};
    int mode=digital?0:ShowChoiceUI("Conversion","Choose where this game is prepared",modes,details,2,L"Choose");if(mode<0)return;
    cJSON *body=cJSON_CreateObject();cJSON_AddStringToObject(body,"release_id",release.c_str());cJSON_AddStringToObject(body,"conversion",mode?"console":"server");
    if(!forTitle.empty())cJSON_AddStringToObject(body,"for_title_id",forTitle.c_str());
    bool licenseChoice=digital;
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
                selected[i]=!forTitle.empty()?(kinds[i]=="dlc"||kinds[i]=="avatar")&&Text(c,"title_id")==forTitle:digital?(kinds[i]=="dlc"||kinds[i]=="xbla"||kinds[i]=="avatar"):(kinds[i]=="god"||kinds[i]=="xbla");
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
            for(int i=0;i<count;++i)if(selected[i]){cJSON_AddItemToArray(chosen,cJSON_CreateString(ids[i].c_str()));any=true;if(kinds[i]=="dlc"||kinds[i]=="xbla"||kinds[i]=="avatar")licenseChoice=true;}
            if(!any){cJSON_Delete(info);cJSON_Delete(body);ShowMessageUI("Nothing selected","Choose at least one component",NULL);return;}
        }
        cJSON_Delete(info);
    }
    if(licenseChoice&&!ChoosePackageLicenses(body)){cJSON_Delete(body);return;}
    char *raw=cJSON_PrintUnformatted(body);
    bool ok=QueuePayload(raw,name,games,content,&error,art,!forTitle.empty());cJSON_free(raw);cJSON_Delete(body);
    if(!ok){ShowMessageUI("Not queued",error.c_str(),NULL);return;}
    ShowMessageUI("Queued in Foundry","Installation continues in the background","Open Queue for progress. B returns to the original store.");
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
static void OnlineUpdatesUI(const char *games,const char *content,unsigned long onlyTitle=0)
{
    cJSON *caps=NULL;if(!UiCall(std::string(API)+"/capabilities","",false,&caps))return;
    bool available=cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(caps,"online_title_updates"));cJSON_Delete(caps);
    if(!available){ShowMessageUI("Title updates","This server does not enable online title-update requests",NULL);return;}
    QueueJobSnapshot snap;if(SnapshotFoundryActiveJob(&snap)&&snap.state!=QUEUE_FINISHED){ShowMessageUI("Foundry","Wait for the active installation to finish",NULL);return;}
    RenderProgressFrame("Title updates","Reading installed game execution IDs",NULL,-1,"Checking library");
    std::vector<InstalledGame> installed(256);int found=EnumerateInstalledGames(games,&installed[0],(int)installed.size(),dprintf);
    std::vector<InstalledIdentity> ids;std::vector<std::string> labels;
    for(int i=0;i<found&&ids.size()<256;++i)
    {
        if(onlyTitle&&installed[i].titleId!=onlyTitle)continue;
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
    char *raw=cJSON_PrintUnformatted(o);std::string error;
    bool ok=QueuePayload(raw,labels[pick]+" - title update",games,content,&error,strtoul(ids[pick].title.c_str(),NULL,16),true);cJSON_free(raw);cJSON_Delete(o);
    if(!ok){ShowMessageUI("Not queued",error.c_str(),NULL);return;}
    ShowMessageUI("Title update queued","Foundry will download and check the exact matching update","Enable it in Aurora after installation. No approximate update will be installed.");
}
void OpenFoundryUI(const char *games,const char *content)
{
    if(!g_started&&!StartFoundry(games,content)){ShowMessageUI("Foundry","Worker could not start",NULL);return;}
    for(;;)
    {
        Device d=DeviceCopy();const char *names[]={"Search games","Search XBLA / DLC","Pair this console","Server address","Resume interrupted installation","Forget console token","Title updates for installed games","Prepared for Xbox","Discard interrupted job"};
        const char *subs[]={"Server or Xbox conversion","Install prepared digital content",d.token.empty()?"Not paired":"Paired",d.origin.c_str(),"Checked files are reused","The website can also revoke access","Exact execution ID match, including Xbox-converted games","Downloads chosen on the website","Only when paused; keeps installed games and the prepared list"};
        int choice=ShowChoiceUI("GOD Foundry","Your server library on the Xbox",names,subs,9,L"Open");if(choice<0)return;
        QueueJobSnapshot activeJob;
        if((choice==2||choice==3||choice==5)&&SnapshotFoundryActiveJob(&activeJob)&&activeJob.state!=QUEUE_FINISHED)
        {ShowMessageUI("Foundry","Pause the installation before changing servers or pairing",NULL);continue;}
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
        if(choice==7){if(OpenFoundryPreparedUI(games,content))return;continue;}
        if(choice==8){QueueJobSnapshot snap;if(SnapshotFoundryActiveJob(&snap)&&snap.state!=QUEUE_FINISHED){ShowMessageUI("Foundry","Pause the active installation first",NULL);continue;}
            if(ShowConfirmUI("Discard interrupted job","Stop resuming the saved installation?","Installed games stay. Existing partial downloads are retained.","Discard")){std::string error;if(!Recover(&error)){ShowMessageUI("Recovery required",error.c_str(),NULL);continue;}DeleteFileA(JOB);DeleteFileA((std::string(JOB)+".old").c_str());SetEvent(g_wake);}continue;}
        if(choice==6){OnlineUpdatesUI(games,content);continue;}
        if(choice==4)
        {
            QueueJobSnapshot snap;bool active=SnapshotFoundryActiveJob(&snap)&&snap.state!=QUEUE_FINISHED;
            if(Exists(JOB)&&!active){EnterCriticalSection(&g_lock);InitSnap(g_work);InterlockedExchange(&g_cancel,0);g_pending=true;LeaveCriticalSection(&g_lock);SetEvent(g_wake);ShowMessageUI("Resuming","Open Queue for progress",NULL);}
            else ShowMessageUI("Foundry",active?"An installation is already active":"No interrupted installation",NULL);
        }
        else BrowseUI(games,content,choice==1);
    }
}


bool FoundryQueuePrepared(const cJSON *reply,const char *games,const char *content,const std::string &only)
{
    std::vector<Work> ready;std::string error;
    if(!ReadPrepared(reply,games,content,&ready)){ShowMessageUI("Prepared for Xbox","The server returned an invalid prepared list",NULL);return false;}
    FilterPrepared(reply,only,&ready);
    if(ready.empty()){ShowMessageUI("Prepared for Xbox","No new ready downloads in this selection","Games must finish preparation first. Avatar unlocks need a compatible signed-in profile; completed items are skipped.");return false;}
    if(!QueuePrepared(ready,&error)){ShowMessageUI("Not queued",error.c_str(),NULL);return false;}
    ShowMessageUI("Saved to download list",Exists(JOB)?"The current installation resumes first; selected items follow":"Games install first, then selected add-ons",FoundryCanUnlockAvatars()?"Open Queue for progress. The saved list survives a restart.":"Open Queue for progress. Avatar unlocks stay prepared until a compatible profile is signed in.");return true;
}
void FoundryPreparedOptionsUI()
{
    const char *names[]={DeviceCopy().autoInbox?"Automatic downloads: ON":"Automatic downloads: OFF","Clear pending download list"};
    const char *subs[]={"Download ready website requests when Omni360 is open and idle","Keeps the active/interrupted installation and server copies"};
    int pick=ShowChoiceUI("Prepared download settings","Manual download is the default",names,subs,2,L"Choose");
    if(pick==0){EnterCriticalSection(&g_lock);g_device.autoInbox=!g_device.autoInbox;if(!SaveDevice()){g_device.autoInbox=!g_device.autoInbox;ShowMessageUI("Settings","Could not save automatic-download preference",NULL);}LeaveCriticalSection(&g_lock);SetEvent(g_wake);}
    else if(pick==1&&ShowConfirmUI("Clear pending list","Remove selections waiting on this Xbox?","Active jobs and server-prepared files stay. Automatic downloads can add ready items again.","Clear"))
    {EnterCriticalSection(&g_lock);std::vector<Work> empty;bool ok=SaveInbox(empty);LeaveCriticalSection(&g_lock);if(!ok)ShowMessageUI("Prepared for Xbox","Could not clear the saved list",NULL);}
}

bool FoundryStoreRequest(const std::string &path,cJSON **reply)
{
    if(!g_started)return false;
    if(DeviceCopy().token.empty())PairUI();
    if(DeviceCopy().token.empty())return false;
    Device d=DeviceCopy();bool catalog=path.compare(0,7,"/store/")==0;std::string key=StoreCacheKey(d,path);
    if(catalog&&StoreCacheRead(key,reply)){dprintf("[foundry] Store page: cache hit\n");return true;}
    DWORD started=GetTickCount();
    if(catalog&&g_storePrefetch&&g_storePrefetchKey==key)
    {
        // A cold or stalled speculative connection must not hold up a click on
        // the warm foreground connection. Give it a short head start, then
        // cancel it and use the independent UI lane. Reap its handle only once
        // the worker has actually exited.
        while(WaitForSingleObject(g_storePrefetch,0)!=WAIT_OBJECT_0)
        {if(GetTickCount()-started>=150){InterlockedExchange(&g_storePrefetchCancel,1);break;}
            if(PollUiInput().pressed&XINPUT_GAMEPAD_B){InterlockedExchange(&g_storePrefetchCancel,1);ResyncUiInput();return false;}Sleep(16);}
        if(WaitForSingleObject(g_storePrefetch,0)==WAIT_OBJECT_0){CloseHandle(g_storePrefetch);g_storePrefetch=NULL;}
        ResyncUiInput();if(StoreCacheRead(key,reply)){dprintf("[foundry] Store page: preload ready in %lu ms\n",GetTickCount()-started);return true;}
    }
    unsigned generation;EnterCriticalSection(&g_lock);generation=g_storeCacheGeneration;LeaveCriticalSection(&g_lock);
    bool ok=UiCall(std::string(API)+path,"",true,reply);if(ok&&catalog)StoreCacheSave(key,*reply,generation);
    if(catalog)dprintf("[foundry] Store page: network %s in %lu ms\n",ok?"ready":"failed",GetTickCount()-started);return ok;
}
void FoundryInstallFromStore(const std::string &release,const std::string &name,const char *games,const char *content,bool digital,const std::string &forTitle,unsigned long art)
{QueueRelease(release,name,games,content,digital,forTitle,art);}
void OpenFoundryUpdatesUI(const char *games,const char *content,unsigned long titleId)
{OnlineUpdatesUI(games,content,titleId);}

void FoundryInstallDlcBatch(const std::vector<std::string> &releases,const std::string &name,const char *games,const char *content,const std::string &forTitle)
{
    QueueJobSnapshot snap;if(SnapshotFoundryActiveJob(&snap)&&snap.state!=QUEUE_FINISHED){ShowMessageUI("Foundry","One Foundry installation is already active","Open Queue for progress.");return;}
    if(releases.empty()||releases.size()>32||!FoundryHex(forTitle,8)){ShowMessageUI("DLC selection","Choose between 1 and 32 DLCs for an identified game",NULL);return;}
    cJSON *caps=NULL;if(!FoundryStoreRequest("/capabilities",&caps))return;
    bool supported=cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(caps,"dlc_multi_select"));cJSON_Delete(caps);
    if(!supported){ShowMessageUI("Server update needed","This Foundry server does not support DLC multi-select yet",NULL);return;}
    cJSON *body=cJSON_CreateObject();cJSON_AddStringToObject(body,"kind","dlc_bundle");cJSON_AddStringToObject(body,"for_title_id",forTitle.c_str());cJSON_AddStringToObject(body,"conversion","server");
    cJSON *ids=cJSON_AddArrayToObject(body,"release_ids");for(size_t i=0;i<releases.size();++i)cJSON_AddItemToArray(ids,cJSON_CreateString(releases[i].c_str()));
    if(!ChoosePackageLicenses(body)){cJSON_Delete(body);return;}
    char *raw=cJSON_PrintUnformatted(body);std::string error;bool ok=QueuePayload(raw,name,games,content,&error,strtoul(forTitle.c_str(),NULL,16),true);cJSON_free(raw);cJSON_Delete(body);
    if(!ok){ShowMessageUI("Not queued",error.c_str(),NULL);return;}
    ShowMessageUI("DLC selection queued","All selected DLCs are prepared as one checked installation","Open Queue for combined progress. Interrupted downloads can be resumed.");
}

void FoundryInstallDiscSet(const std::vector<std::string> &releases,const std::string &name,const char *games,const char *content,bool contentPackages,unsigned long art)
{
    if(releases.empty()||releases.size()>8)return;
    QueueJobSnapshot snap;if(SnapshotFoundryActiveJob(&snap)&&snap.state!=QUEUE_FINISHED){ShowMessageUI("Foundry","An installation is already active","Open Queue for progress.");return;}
    cJSON *caps=NULL;if(!FoundryStoreRequest("/capabilities",&caps))return;bool supported=cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(caps,"disc_multi_select"));cJSON_Delete(caps);
    if(!supported){ShowMessageUI("Server update needed","This server does not support checked disc sets yet",NULL);return;}
    cJSON *body=cJSON_CreateObject();cJSON_AddStringToObject(body,"kind","disc_bundle");cJSON_AddStringToObject(body,"conversion","server");
    cJSON *ids=cJSON_AddArrayToObject(body,"release_ids");for(size_t i=0;i<releases.size();++i)cJSON_AddItemToArray(ids,cJSON_CreateString(releases[i].c_str()));
    if(contentPackages){if(!ChoosePackageLicenses(body)){cJSON_Delete(body);return;}}
    else cJSON_AddBoolToObject(body,"unlock_packages",false);
    char *raw=cJSON_PrintUnformatted(body);std::string error;bool ok=QueuePayload(raw,name,games,content,&error,art,contentPackages);cJSON_free(raw);cJSON_Delete(body);
    if(!ok){ShowMessageUI("Not queued",error.c_str(),NULL);return;}
    ShowMessageUI("Disc set queued","All selected discs form one checked, resumable installation","Open Queue for combined file counts and progress.");
}
