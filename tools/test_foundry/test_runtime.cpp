// Executes the real Foundry transfer/job/commit code with a host filesystem
// adapter and a fault-injected HTTP transport. This is not an Xbox SDK build.
#include "shim/xtl.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include <dirent.h>
#include <errno.h>
#include <fstream>
#include <sstream>
#include <iostream>
#include <map>
#include <vector>
#include <algorithm>

static std::string hostRoot;
static unsigned long long diskAvailable=100ULL*1024*1024*1024;
static unsigned diskQueries=0;
static int crashAfterMove=-1;
struct PowerLoss {};
static std::string MapPath(std::string p)
{
    std::replace(p.begin(),p.end(),'\\','/');size_t colon=p.find(':');
    if(colon!=std::string::npos)p=hostRoot+"/"+p.substr(0,colon)+p.substr(colon+1);
    return p;
}
static FILE *host_fopen(const char *p,const char *mode){return fopen(MapPath(p).c_str(),mode);}
#define fopen host_fopen
#include "../../src/Foundry.cpp"
#undef fopen

struct HostHandle
{
    FILE *file;std::vector<std::string> names;size_t index;std::string directory;
    HostHandle():file(NULL),index(0){}
};
int test_snprintf(char *out,size_t n,const char *format,...)
{
    std::string f=format;size_t at;
    while((at=f.find("I64"))!=std::string::npos)f.replace(at,3,"ll");
    va_list args;va_start(args,format);int result=vsnprintf(out,n,f.c_str(),args);va_end(args);return result;
}
extern "C" void dprintf(const char*,...){}
extern "C" DWORD GetFileAttributesA(const char *p)
{struct stat s;if(stat(MapPath(p).c_str(),&s))return INVALID_FILE_ATTRIBUTES;return S_ISDIR(s.st_mode)?FILE_ATTRIBUTE_DIRECTORY:FILE_ATTRIBUTE_NORMAL;}
extern "C" BOOL CreateDirectoryA(const char *p,void*){return mkdir(MapPath(p).c_str(),0700)==0;}
static void FillFind(HostHandle *h,WIN32_FIND_DATAA *out)
{memset(out,0,sizeof(*out));strncpy(out->cFileName,h->names[h->index].c_str(),sizeof(out->cFileName)-1);struct stat s;stat((h->directory+"/"+h->names[h->index]).c_str(),&s);out->dwFileAttributes=S_ISDIR(s.st_mode)?FILE_ATTRIBUTE_DIRECTORY:FILE_ATTRIBUTE_NORMAL;}
extern "C" HANDLE FindFirstFileA(const char *p,WIN32_FIND_DATAA *out)
{
    HostHandle *h=new HostHandle;h->directory=MapPath(p);h->directory.resize(h->directory.size()-2);
    DIR *d=opendir(h->directory.c_str());if(!d){delete h;return INVALID_HANDLE_VALUE;}
    struct dirent *entry;while((entry=readdir(d)))if(strcmp(entry->d_name,".")&&strcmp(entry->d_name,".."))h->names.push_back(entry->d_name);closedir(d);
    std::sort(h->names.begin(),h->names.end());if(h->names.empty()){delete h;return INVALID_HANDLE_VALUE;}FillFind(h,out);return h;
}
extern "C" BOOL FindNextFileA(HANDLE handle,WIN32_FIND_DATAA *out)
{HostHandle *h=(HostHandle*)handle;if(++h->index>=h->names.size())return FALSE;FillFind(h,out);return TRUE;}
extern "C" BOOL FindClose(HANDLE h){delete (HostHandle*)h;return TRUE;}
extern "C" BOOL RemoveDirectoryA(const char *p){return rmdir(MapPath(p).c_str())==0;}
extern "C" BOOL DeleteFileA(const char *p){return unlink(MapPath(p).c_str())==0;}
extern "C" BOOL MoveFileA(const char *a,const char *b)
{
    struct stat s;if(stat(MapPath(b).c_str(),&s)==0)return FALSE;
    bool ok=rename(MapPath(a).c_str(),MapPath(b).c_str())==0;
    if(ok&&std::string(a).compare(0,5,"Hdd1:")==0&&crashAfterMove>0&&--crashAfterMove==0)throw PowerLoss();
    return ok;
}
extern "C" HANDLE CreateFileA(const char *p,DWORD,DWORD,void*,DWORD,DWORD,HANDLE)
{HostHandle *h=new HostHandle;h->file=host_fopen(p,"wb");if(!h->file){delete h;return INVALID_HANDLE_VALUE;}return h;}
extern "C" BOOL WriteFile(HANDLE p,const void *data,DWORD n,DWORD *written,void*)
{*written=(DWORD)fwrite(data,1,n,((HostHandle*)p)->file);return *written==n;}
extern "C" BOOL FlushFileBuffers(HANDLE p)
{FILE *f=((HostHandle*)p)->file;return fflush(f)==0&&fsync(fileno(f))==0;}
extern "C" BOOL CloseHandle(HANDLE p)
{if((size_t)p<=2)return TRUE;HostHandle *h=(HostHandle*)p;if(h->file)fclose(h->file);delete h;return TRUE;}
extern "C" void InitializeCriticalSection(CRITICAL_SECTION*){}
extern "C" void DeleteCriticalSection(CRITICAL_SECTION*){}
extern "C" void EnterCriticalSection(CRITICAL_SECTION*){}
extern "C" void LeaveCriticalSection(CRITICAL_SECTION*){}
extern "C" LONG InterlockedExchange(volatile LONG *p,LONG v){LONG old=*p;*p=v;return old;}
extern "C" LONG InterlockedCompareExchange(volatile LONG *p,LONG v,LONG compare){LONG old=*p;if(old==compare)*p=v;return old;}
extern "C" HANDLE CreateMutexA(void*,BOOL,const char*){return (HANDLE)1;}
extern "C" BOOL ReleaseMutex(HANDLE){return TRUE;}
extern "C" BOOL SetEvent(HANDLE){return TRUE;}
extern "C" DWORD WaitForSingleObject(HANDLE,DWORD){return WAIT_OBJECT_0;}
extern "C" void Sleep(DWORD){}
extern "C" DWORD GetTickCount(){static DWORD n=0;return n+=100;}
extern "C" BOOL GetDiskFreeSpaceExA(const char*,ULARGE_INTEGER *a,ULARGE_INTEGER*,ULARGE_INTEGER*){++diskQueries;a->QuadPart=diskAvailable;return TRUE;}
extern "C" BOOL GetFileAttributesExA(const char *p,int,WIN32_FILE_ATTRIBUTE_DATA *out)
{struct stat s;if(stat(MapPath(p).c_str(),&s))return FALSE;out->nFileSizeHigh=(DWORD)((unsigned long long)s.st_size>>32);out->nFileSizeLow=(DWORD)s.st_size;return TRUE;}

static void Put(const std::string &p,const std::string &bytes)
{assert(Parents(p));FILE *f=host_fopen(p.c_str(),"wb");assert(f);assert(fwrite(bytes.data(),1,bytes.size(),f)==bytes.size());fclose(f);}
static std::string Get(const std::string &p)
{std::ifstream f(MapPath(p).c_str(),std::ios::binary);std::ostringstream s;s<<f.rdbuf();return s.str();}
static void Case(const char *name)
{
    hostRoot=std::string(getenv("FOUNDRY_TEST_ROOT"))+"/"+name;assert(mkdir(hostRoot.c_str(),0700)==0);
    assert(mkdir((hostRoot+"/game").c_str(),0700)==0);assert(mkdir((hostRoot+"/Hdd1").c_str(),0700)==0);
    g_stop=g_cancel=0;g_started=true;g_used=g_pending=false;crashAfterMove=-1;
    g_transferBase=g_transferTotal=g_transferJobDone=g_transferJobTotal=0;g_transferFile=g_transferFiles=0;
    diskAvailable=100ULL*1024*1024*1024;diskQueries=0;
    g_device=Device();g_device.id=std::string(32,'a');g_device.token="fg_"+std::string(43,'b');g_device.sequence=0;
}
static std::string Header()
{
    std::string h(0xB000,'\0');memcpy(&h[0],"LIVE",4);
    const unsigned char type[]={0,0,0x70,0},title[]={0x41,0x56,0x07,0xE1},media[]={0x12,0x34,0x56,0x78};
    memcpy(&h[0x344],type,4);memcpy(&h[0x360],title,4);memcpy(&h[0x354],media,4);return h;
}
static std::string netData;
static std::vector<unsigned> cuts;
static std::vector<unsigned long long> offsets;
static int postLosses=0,posts=0,serverCreates=0;
static bool cancelTransfer=false,firstPostWasDurable=false;
static std::map<std::string,std::string> serverRequests;
static std::string HeaderValue(const std::string &s,const std::string &key)
{size_t at=s.find(key+": ");if(at==std::string::npos)return "";at+=key.size()+2;return s.substr(at,s.find("\r\n",at)-at);}
int httpRequestHTTPS(const std::string,HttpMethod method,const char *body,const char *rawHeaders,const std::string filename,
                     char *buffer,unsigned long long *capacity,bool file,char*,unsigned long long,void(const char*,...),
                     unsigned long long expected,DownloadProgressFn progress,bool append,const HttpResponsePolicy *policy)
{
    std::string headers=rawHeaders?rawHeaders:"";
    if(method==HTTP_POST)
    {
        ++posts;std::string key=HeaderValue(headers,"Idempotency-Key");assert(!key.empty());
        Work saved;firstPostWasDurable=LoadWork(&saved)&&saved.body==body&&saved.key==key;assert(firstPostWasDurable);
        if(!serverRequests.count(key)){serverRequests[key]=body;++serverCreates;}else assert(serverRequests[key]==body);
        if(postLosses-->0){*capacity=0;return -1;}
        std::string reply="{\"id\":\"cccccccccccccccccccccccccccccccc\"}";assert(*capacity>=reply.size());memcpy(buffer,reply.data(),reply.size());*capacity=reply.size();return 200;
    }
    assert(file&&policy&&policy->status==206);
    std::string range=HeaderValue(headers,"Range");unsigned long long start=0,end=0;
    assert(sscanf(range.c_str(),"bytes=%llu-%llu",&start,&end)==2&&start<=end&&end<netData.size());
    assert(expected==end-start+1);assert(std::string(policy->contentRange)=="bytes "+Decimal(start)+"-"+Decimal(end)+"/"+Decimal(netData.size()));
    assert(HeaderValue(headers,"If-Range")==policy->etag);offsets.push_back(start);
    unsigned long long n=expected;
    if(!cuts.empty()){n=std::min(n,(unsigned long long)cuts[0]);cuts.erase(cuts.begin());}
    FILE *f=host_fopen(filename.c_str(),append?"ab":"wb");assert(f);assert(fwrite(netData.data()+(size_t)start,1,(size_t)n,f)==n);fclose(f);
    if(progress)progress(n,expected,100,1);
    if(cancelTransfer){g_cancel=1;return HTTP_STATUS_CANCELLED;}
    return n==expected?206:-1;
}

static void Transfers()
{
    Case("transfers");netData="a real interrupted transfer with checked bytes";offsets.clear();cuts.clear();cuts.push_back(7);cuts.push_back(5);
    const std::string dest="Hdd1:\\Content\\_Foundry\\"+std::string(32,'d')+"\\files\\415607E1\\00000002\\package";
    Put("game:\\expected",netData);std::string sha,error;assert(HashFile("game:\\expected",&sha,netData.size()));
    assert(Fetch("/api/console/v1/artifacts/id/files/"+std::string(32,'e'),dest,netData.size(),sha,&error));
    assert(Get(dest)==netData&&offsets.size()==3&&offsets[0]==0&&offsets[1]==7&&offsets[2]==12);
    // A power-cycle/cancel preserves a short partial and resumes its suffix.
    Case("cancel-resume");offsets.clear();cuts.clear();cuts.push_back(9);cancelTransfer=true;
    assert(!Fetch("/api/console/v1/artifacts/id/files/"+std::string(32,'e'),dest,netData.size(),sha,&error));
    assert(!Exists(dest));cancelTransfer=false;g_cancel=0;
    assert(Fetch("/api/console/v1/artifacts/id/files/"+std::string(32,'e'),dest,netData.size(),sha,&error));
    assert(offsets[1]==9&&Get(dest)==netData);
    assert(g_snap.bytesDone==netData.size()&&g_snap.bytesTotal==netData.size());
    // Source-piece ranges resume inside a piece at a nonzero archive offset.
    Case("source-piece");offsets.clear();cuts.clear();cuts.push_back(3);
    std::string partial="Hdd1:\\source\\P00001.bin.partial";
    assert(RangeDownload("/api/console/v1/source-artifacts/id/file",partial,netData.size(),10,20,sha,&error));
    assert(offsets[0]==10&&offsets[1]==13&&Get(partial)==netData.substr(10,20));
    // Bad checksums never replace an existing good destination.
    Case("checksum");Put(dest,"previous valid package");cuts.clear();offsets.clear();
    assert(!Fetch("/api/console/v1/artifacts/id/files/"+std::string(32,'e'),dest,netData.size(),std::string(64,'0'),&error));
    assert(Get(dest)=="previous valid package");
    // A durable length with damaged prefix bytes is repaired automatically.
    Case("damaged-prefix");cuts.clear();offsets.clear();
    std::string part="Hdd1:\\Content\\_Foundry\\"+std::string(32,'d')+"\\parts\\"+std::string(32,'e')+".part";
    Put(part,"BAD PREFIX");
    assert(Fetch("/api/console/v1/artifacts/id/files/"+std::string(32,'e'),dest,netData.size(),sha,&error));
    assert(offsets.size()==2&&offsets[0]==10&&offsets[1]==0&&Get(dest)==netData);
}
static void PendingJob()
{
    Case("pending-job");serverRequests.clear();serverCreates=posts=0;postLosses=6;
    Work w;w.games=w.content="Hdd1:\\Content\\0000000000000000";w.name="Synthetic game";w.body="{\"release_id\":\"catalog-1\",\"conversion\":\"server\"}";
    std::string error;assert(NewRequestKey(&w,&error));std::string originalKey=w.key;
    assert(!CreatePendingRequest(&w,&error));assert(serverCreates==1&&posts==6);
    // Simulate process restart by discarding all in-memory work.
    Work restarted;assert(LoadWork(&restarted));assert(restarted.request.empty()&&restarted.key==originalKey&&restarted.body==w.body);
    postLosses=0;assert(CreatePendingRequest(&restarted,&error));assert(serverCreates==1&&posts==7);
    Work persisted;assert(LoadWork(&persisted));assert(persisted.request==std::string(32,'c'));
    g_device.origin="https://another.example.com";assert(!CreatePendingRequest(&persisted,&error));assert(posts==7);
}
static void Transactions()
{
    for(int cut=0;cut<=4;++cut)
    {
        char label[32];snprintf(label,sizeof(label),"transaction-%d",cut);Case(label);
        Work w;w.games=w.content="Hdd1:\\Content\\0000000000000000";
        FoundryManifest manifest;manifest.id=std::string(32,'d');FoundryComponent component;component.kind="god";component.titleId="415607E1";component.mediaId="12345678";
        FoundryFile file;file.path="415607E1/00007000/new";file.role="header";component.files.push_back(file);manifest.components.push_back(component);
        std::string old=w.games+"\\415607E1\\00007000\\old",current=w.games+"\\415607E1\\00007000\\new";
        std::string staged=Stage(w.games,manifest.id)+"\\files\\415607E1\\00007000\\new";
        Put(old,Header()+"OLD");Put(old+".data\\Data0000","OLD DATA");Put(staged,Header()+"NEW");Put(staged+".data\\Data0000","NEW DATA");
        Put(w.games+"\\415607E1\\00000002\\unrelated","KEEP DLC");crashAfterMove=cut?cut:-1;
        std::string error;
        try{assert(Commit(w,manifest,&error));assert(cut==0);}catch(const PowerLoss&){assert(cut>0);}
        crashAfterMove=-1;
        if(cut){assert(Recover(&error));assert(Get(old)==Header()+"OLD");assert(Get(old+".data\\Data0000")=="OLD DATA");assert(!Exists(current));}
        else{assert(Get(current)==Header()+"NEW");assert(Get(current+".data\\Data0000")=="NEW DATA");assert(!Exists(old));}
        assert(Get(w.games+"\\415607E1\\00000002\\unrelated")=="KEEP DLC");
    }
    // Interrupted state-file replacement restores the previous complete file.
    Case("state-generation");cJSON *o=cJSON_CreateObject();cJSON_AddStringToObject(o,"test","saved");assert(WriteJson(JOB,o));cJSON_Delete(o);
    assert(MoveFileA(JOB,(std::string(JOB)+".old").c_str()));o=ReadJson(JOB);assert(Text(o,"test")=="saved");cJSON_Delete(o);
}
static void CacheAndSpace()
{
    for(int existing=0;existing<=1;++existing)for(int cut=0;cut<=existing+1;++cut)
    {
        char label[40];snprintf(label,sizeof(label),"cache-%d-%d",existing,cut);Case(label);
        Work w;w.games=w.content="Hdd1:\\Content\\0000000000000000";
        FoundryManifest m;m.id=std::string(32,'d');FoundryComponent c;c.kind="title_update";
        FoundryFile f;f.root="cache";f.path="TU_"+std::string(32,'A');c.files.push_back(f);m.components.push_back(c);
        std::string target="Hdd1:\\Cache\\"+f.path,stage=Stage("Hdd1:\\Cache",m.id)+"\\files\\"+f.path;
        if(existing)Put(target,"OLD TU");Put(stage,"NEW TU");Put("Hdd1:\\Cache\\TU_unrelated","KEEP");
        crashAfterMove=cut?cut:-1;std::string error;
        try{assert(Commit(w,m,&error));assert(cut==0);}catch(const PowerLoss&){assert(cut>0);}
        crashAfterMove=-1;
        if(cut){assert(Recover(&error));assert(existing?Get(target)=="OLD TU":!Exists(target));assert(Get(stage)=="NEW TU");}
        else assert(Get(target)=="NEW TU");
        assert(Get("Hdd1:\\Cache\\TU_unrelated")=="KEEP");
        assert(!JournalPath("_Foundry/"+std::string(32,'e')+"/files/"+f.path,"cache",m.id));
        assert(!JournalPath("415607E1/000B0000/file","cache",m.id));
    }
    Case("shared-volume-budget");Work w;w.games="Hdd1:\\Games";w.content="HDD1:\\Content\\0000000000000000";
    FoundryManifest m;m.id=std::string(32,'d');
    for(int i=0;i<3;++i){FoundryComponent c;c.kind=i==0?"god":i==1?"dlc":"title_update";FoundryFile f;f.root=i==2?"cache":"content";f.path=i==2?"TU_test":"415607E1/00007000/file";f.id=std::string(32,'a'+i);f.bytes=1024ULL*1024*1024;c.files.push_back(f);m.components.push_back(c);}
    diskAvailable=3ULL*1024*1024*1024;std::string error;
    assert(!CheckRequiredSpace(w,m,&error)&&error.find("Not enough free space")!=std::string::npos);
    assert(diskQueries==1);diskQueries=0;w.games="Usb0:\\Games";
    assert(CheckRequiredSpace(w,m,&error)&&diskQueries==2);
}
static void AggregateProgress()
{
    Case("aggregate-progress");
    BeginTransferFile(12,47,11000,47000,"Data0010");
    g_transferBase=200;DownloadProgress(300,800,100,5);
    assert(g_snap.bytesDone==11500&&g_snap.bytesTotal==47000&&g_snap.secondsLeft==355);
    assert(std::string(g_snap.phase).find("12/47 (35 left)")!=std::string::npos);
    TransferFilePhase(true);assert(std::string(g_snap.phase).find("Retry file")!=std::string::npos);
    BeginTransferFile(3,4,64ULL*1024*1024,100ULL*1024*1024,"Archive part 3");
    g_transferBase=1024;DownloadProgress(1024,32ULL*1024*1024,1024,1);
    assert(g_snap.bytesDone==64ULL*1024*1024+2048&&g_snap.secondsLeft==36862);
    DWORD start=GetTickCount();for(int i=0;i<10;++i)GetTickCount();
    TimedProgress("Converting on Xbox",1000,5000,start);
    assert(g_snap.bytesDone==1000&&g_snap.bytesTotal==5000&&g_snap.bytesPerSec>0&&g_snap.secondsLeft>0);
}
static void CatalogCache()
{
    Case("catalog-cache");ClearFoundryStoreCache();
    Device d=DeviceCopy();std::string key=StoreCacheKey(d,"/store/home");
    cJSON *o=cJSON_CreateObject();cJSON_AddStringToObject(o,"test","catalog");
    unsigned generation=g_storeCacheGeneration;StoreCacheSave(key,o,generation);cJSON *reply=NULL;
    assert(StoreCacheRead(key,&reply)&&Text(reply,"test")=="catalog");cJSON_Delete(reply);
    Device other=d;other.origin="https://other.example.com";assert(!StoreCacheRead(StoreCacheKey(other,"/store/home"),NULL));
    other=d;other.token="another-device-token";assert(!StoreCacheRead(StoreCacheKey(other,"/store/home"),NULL));
    ClearFoundryStoreCache();StoreCacheSave(key,o,generation);assert(!StoreCacheRead(key,NULL));
    StoreCacheSave(key,o,g_storeCacheGeneration);for(int i=0;i<610;++i)GetTickCount();assert(!StoreCacheRead(key,NULL));
    std::string large(200*1024,'a');cJSON_AddStringToObject(o,"body",large.c_str());
    for(int i=0;i<20;++i){char id[16];snprintf(id,sizeof(id),"page-%d",i);StoreCacheSave(id,o,g_storeCacheGeneration);}
    assert(g_storeCache.size()<=16&&g_storeCacheBytes<=2*1024*1024);
    assert(StoreCacheRead("page-19",NULL)&&!StoreCacheRead("page-0",NULL));
    ClearFoundryStoreCache();assert(g_storeCache.empty()&&g_storeCacheBytes==0);cJSON_Delete(o);
}
int main()
{
    assert(getenv("FOUNDRY_TEST_ROOT"));Transfers();PendingJob();Transactions();CacheAndSpace();AggregateProgress();CatalogCache();
    std::cout<<"Real client runtime: interrupted transfers, in-piece resume, lost POST/reboot idempotency, cross-server guard, GoD/Cache TU power cuts and shared-volume space checks passed\n";
}
