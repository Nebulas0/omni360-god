// Fault-injected transport for the actual reusable-session implementation.
// run.py inserts the unchanged session section from downloadFile.cpp below.
#include "downloadFile.h"
#include "HttpResponse.h"
#include <algorithm>
#include <assert.h>
#include <cstring>
#include <cstdlib>
#include <iostream>
#include <string>
#include <strings.h>
#include <vector>
#include <stdio.h>
#define _snprintf snprintf
#define _stricmp strcasecmp
#define ERROR(x) ((void)0)
#define XNET_STARTUP_BYPASS_SECURITY 1
#define MAKEWORD(a,b) 0
struct XNetStartupParams { int cfgSizeOfStruct,cfgFlags; };
struct WSADATA {};
static int XNetStartup(XNetStartupParams*) { return 0; }
static int WSAStartup(int,WSADATA*) { return 0; }
static void WSACleanup() {}
static void XNetCleanup() {}
static void Sleep(unsigned) {}
static int parseURL(const char *url,char *domain,char *path)
{
    std::string u=url;assert(u.compare(0,8,"https://")==0);size_t at=u.find('/',8);
    strcpy(domain,u.substr(8,at-8).c_str());strcpy(path,u.substr(at).c_str());return 0;
}
static void LogEscapePercent(const char *in,char *out,size_t n) { snprintf(out,n,"%s",in); }
static const char *UserAgentFor(const char*) { return "session-test"; }
static void Quiet(const char*,...) {}
struct Connection { std::string input;size_t at;Connection():at(0){} };
struct XboxTLSContext { Connection *internal; };
static std::vector<std::string> responses,requests;
static unsigned connects=0,frees=0;
static bool failWrite=false,cancel=false;
static int cancelAfterReads=-1;
static bool XboxTLS_CreateContext(XboxTLSContext *c,const char*) { c->internal=new Connection;return true; }
static int addTrustAnchors(XboxTLSContext*) { return EXIT_SUCCESS; }
static bool ResolveDNS(const char*,char *ip,size_t) { strcpy(ip,"127.0.0.1");return true; }
static int searchDnsCache(const char*,char*,size_t) { return 1; }
static bool XboxTLS_Connect(XboxTLSContext*,const char*,const char*,int) { ++connects;return true; }
static void XboxTLS_Free(XboxTLSContext *c) { ++frees;delete c->internal;c->internal=NULL; }
static int XboxTLS_Write(XboxTLSContext *c,const void *p,int n)
{
    if(failWrite){failWrite=false;return -1;}
    requests.push_back(std::string((const char*)p,n));assert(!responses.empty());
    c->internal->input+=responses.front();responses.erase(responses.begin());return n;
}
static int XboxTLS_Read(XboxTLSContext *c,void *p,int n)
{
    Connection &v=*c->internal;size_t take=std::min((size_t)n,std::min((size_t)7,v.input.size()-v.at));
    if(cancelAfterReads>0&&--cancelAfterReads==0)cancel=true;
    memcpy(p,v.input.data()+v.at,take);v.at+=take;return (int)take;
}
// INSERT_ACTUAL_SESSION
static std::string Response(const std::string &body,const std::string &headers="",int status=200)
{
    char line[100];snprintf(line,sizeof(line),"HTTP/1.1 %d Reply\r\nContent-Length: %u\r\n",status,(unsigned)body.size());
    return std::string(line)+headers+"\r\n"+body;
}
static bool Progress(unsigned long long,unsigned long long,unsigned long long,unsigned long long) { return !cancel; }
int main()
{
    HttpsSession *s=HttpsSessionOpen(Quiet);assert(s);char buf[65];unsigned long long n;
    for(int i=0;i<2;++i){responses.push_back(Response("{\"ok\":true}"));n=64;assert(HttpsSessionGet(s,"https://foundry.test/page","Authorization: Bearer private\r\n",buf,&n,true,Progress)==200);assert(n==11);}
    assert(connects==1&&requests.size()==2&&requests[1].find("Connection: keep-alive")!=std::string::npos);
    // An idle connection closed before a response is transparently retried.
    responses.push_back("");responses.push_back(Response("{}"));n=64;
    assert(HttpsSessionGet(s,"https://foundry.test/next",NULL,buf,&n,true,Progress)==200&&connects==2);
    failWrite=true;responses.push_back(Response("{}"));n=64;
    assert(HttpsSessionGet(s,"https://foundry.test/next",NULL,buf,&n,true,Progress)==200&&connects==3);
    // JSON errors remain available to the UI and don't corrupt the next GET.
    responses.push_back(Response("{\"error\":\"Pair again\"}","",401));n=64;
    assert(HttpsSessionGet(s,"https://foundry.test/page",NULL,buf,&n,true,Progress)==401&&strstr(buf,"Pair again"));
    unsigned sent=(unsigned)requests.size();responses.push_back(Response("","Location: https://elsewhere.test/\r\n",302));n=64;
    assert(HttpsSessionGet(s,"https://foundry.test/page","Authorization: Bearer private\r\n",buf,&n,true,Progress)==0);
    assert(requests.size()==sent+1&&responses.empty()&&!s->connected);
    responses.push_back(Response("{}","Content-Encoding: gzip\r\n"));n=64;
    assert(HttpsSessionGet(s,"https://foundry.test/page",NULL,buf,&n,true,Progress)==0&&!s->connected);
    responses.push_back(Response(std::string(65,'x')));n=64;
    assert(HttpsSessionGet(s,"https://foundry.test/page",NULL,buf,&n,true,Progress)==-1&&!s->connected);
    responses.push_back("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n");n=64;
    assert(HttpsSessionGet(s,"https://foundry.test/page",NULL,buf,&n,true,Progress)==200&&n==2&&s->connected);
    sent=(unsigned)requests.size();cancel=true;n=64;
    assert(HttpsSessionGet(s,"https://foundry.test/page",NULL,buf,&n,true,Progress)==HTTP_STATUS_CANCELLED);
    assert(!s->connected&&requests.size()==sent);cancel=false;
    responses.push_back(Response("{}"));n=64;cancelAfterReads=2;
    assert(HttpsSessionGet(s,"https://foundry.test/page",NULL,buf,&n,true,Progress)==HTTP_STATUS_CANCELLED&&!s->connected);
    cancel=false;cancelAfterReads=-1;
    responses.push_back(Response("{}","Connection: close\r\n"));n=64;
    assert(HttpsSessionGet(s,"https://foundry.test/page",NULL,buf,&n,true,Progress)==200&&!s->connected);
    // Existing Archive.org callers retain redirect and non-JSON behavior.
    responses.push_back(Response("","Location: https://archive.test/file\r\n",302));n=64;
    assert(HttpsSessionGet(s,"https://archive.test/page",NULL,buf,&n)==302&&std::string(buf)=="https://archive.test/file");
    responses.push_back(Response("bad","",403));n=64;
    assert(HttpsSessionGet(s,"https://archive.test/page",NULL,buf,&n)==403);
    HttpsSessionClose(s);assert(frees==connects&&responses.empty());
    std::cout<<"Actual HTTPS session: reuse, idle reconnect, JSON errors, redirects, encoding, overflow, chunks, cancellation and legacy callers passed\n";
}
