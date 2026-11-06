#include "FoundryProtocol.h"
#include "cJSON.h"
#include <assert.h>
#include <fstream>
#include <sstream>
#include <iostream>

static std::string Load(const std::string &p)
{std::ifstream f(p.c_str());std::ostringstream s;s<<f.rdbuf();return s.str();}
static bool Parse(const std::string &s)
{FoundryManifest m;std::string error;return FoundryParseManifest(s.c_str(),&m,&error);}
static bool Changed(cJSON *o)
{char *s=cJSON_PrintUnformatted(o);bool result=Parse(s);cJSON_free(s);return result;}
int main(int argc,char **argv)
{
    assert(argc==2);std::string dir=argv[1];
    unsigned long long n=0;
    assert(FoundryDecimal("8589934592",&n)&&n==8589934592ULL);
    assert(!FoundryDecimal("18446744073709551616",&n));
    assert(!FoundryDecimal("-1",&n));assert(!FoundryDecimal("4.0",&n));assert(!FoundryDecimal("",&n));
    assert(FoundrySafePath("415607E1/00007000/12345678.data/Data0000"));
    const char *bad[]={"../bad","/absolute","a//b","a/../b","a\\b","a:b","a/trailing.","a/end ","a/*","a/"};
    for(unsigned i=0;i<sizeof(bad)/sizeof(bad[0]);++i)assert(!FoundrySafePath(bad[i]));
    assert(FoundryOrigin("https://iso2god.mediahub.stream"));
    assert(!FoundryOrigin("http://example.org"));assert(!FoundryOrigin("https://evil.org/path"));
    assert(!FoundryOrigin("https://user@evil.org"));assert(!FoundryOrigin("https://evil.org:1234"));
    std::string prepared=Load(dir+"/prepared.json");assert(Parse(prepared));
    FoundryManifest m;std::string error;assert(FoundryParseManifest(prepared.c_str(),&m,&error));
    assert(m.components.size()==1&&m.components[0].files.size()==2&&m.totalBytes>0);
    cJSON *o=cJSON_Parse(prepared.c_str());
    cJSON_ReplaceItemInObjectCaseSensitive(o,"schema_version",cJSON_CreateNumber(2));assert(!Changed(o));cJSON_Delete(o);
    o=cJSON_Parse(prepared.c_str());cJSON_ReplaceItemInObjectCaseSensitive(o,"provider_id",cJSON_CreateString("other"));assert(!Changed(o));cJSON_Delete(o);
    o=cJSON_Parse(prepared.c_str());cJSON *files=cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(cJSON_GetObjectItemCaseSensitive(o,"components"),0),"files");
    cJSON *file=cJSON_GetArrayItem(files,0);
    cJSON_ReplaceItemInObjectCaseSensitive(file,"relative_path",cJSON_CreateString("../escape"));assert(!Changed(o));cJSON_Delete(o);
    o=cJSON_Parse(prepared.c_str());files=cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(cJSON_GetObjectItemCaseSensitive(o,"components"),0),"files");
    cJSON_AddItemToArray(files,cJSON_Duplicate(cJSON_GetArrayItem(files,0),1));assert(!Changed(o));cJSON_Delete(o);
    o=cJSON_Parse(prepared.c_str());files=cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(cJSON_GetObjectItemCaseSensitive(o,"components"),0),"files");file=cJSON_GetArrayItem(files,0);
    cJSON_ReplaceItemInObjectCaseSensitive(file,"bytes",cJSON_CreateString("4294967296"));assert(!Changed(o));cJSON_Delete(o);
    assert(Parse(Load(dir+"/source-stored.json")));assert(Parse(Load(dir+"/source-deflate.json")));
    o=cJSON_Parse(Load(dir+"/source-deflate.json").c_str());cJSON *source=cJSON_GetObjectItemCaseSensitive(o,"source");
    cJSON_ReplaceItemInObjectCaseSensitive(source,"bytes",cJSON_CreateString("8589934592"));assert(Changed(o));
    cJSON_ReplaceItemInObjectCaseSensitive(source,"data_offset",cJSON_CreateString("8589934592"));assert(!Changed(o));cJSON_Delete(o);
    assert(!Parse("{}"));assert(!Parse("{not json"));
    std::cout<<"Foundry C++03 contract, path, origin, overflow and manifest checks passed\n";
}
