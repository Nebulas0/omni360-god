#include "FoundryProtocol.h"
#include "cJSON.h"
#include <set>
#include <string.h>
#include <stdlib.h>

bool FoundryHex(const std::string &s, unsigned count)
{
    if (s.size() != count) return false;
    for (size_t i = 0; i < s.size(); ++i)
        if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f') || (s[i] >= 'A' && s[i] <= 'F'))) return false;
    return true;
}

bool FoundryDecimal(const char *s, unsigned long long *out)
{
    if (!s || !*s || strlen(s) > 20) return false;
    unsigned long long n = 0;
    for (; *s; ++s)
    {
        if (*s < '0' || *s > '9' || n > (0x7FFFFFFFFFFFFFFFULL - (*s - '0')) / 10) return false;
        n = n * 10 + (*s - '0');
    }
    *out = n; return true;
}

bool FoundrySafePath(const std::string &s)
{
    if (s.empty() || s.size() > 200 || s[0] == '/' || s[s.size()-1] == '/') return false;
    size_t start = 0;
    for (size_t i = 0; i <= s.size(); ++i)
    {
        if (i == s.size() || s[i] == '/')
        {
            std::string p = s.substr(start, i-start);
            if (p.empty() || p.size() > 42 || p == "." || p == ".." || p[p.size()-1] == '.' || p[p.size()-1] == ' ') return false;
            start = i + 1;
        }
        else if ((unsigned char)s[i] < 32 || (unsigned char)s[i] > 126 || strchr("<>:\"\\|?*", s[i])) return false;
    }
    return true;
}

bool FoundryOrigin(const std::string &s)
{
    if (s.compare(0,8,"https://") || s.size() < 9 || s.size() > 200) return false;
    // Deliberately use a DNS hostname and default TLS port; upstream parseURL
    // does not implement arbitrary port/IPv6/userinfo handling reliably.
    for (size_t i=8; i<s.size(); ++i)
        if (!((s[i]>='a'&&s[i]<='z') || (s[i]>='A'&&s[i]<='Z') || (s[i]>='0'&&s[i]<='9') || s[i]=='.' || s[i]=='-')) return false;
    return s[8]!='.' && s[s.size()-1]!='.';
}

static std::string Text(const cJSON *object, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(object,key);
    return cJSON_IsString(v) && v->valuestring ? v->valuestring : "";
}

bool FoundryRetryableStatus(int status)
{
    return status==0 || status==-1 || status==408 || status==425 || status==429 || (status>=500 && status<=599);
}

static bool Number(const cJSON *o, const char *key, unsigned long long *out)
{
    std::string v=Text(o,key); return FoundryDecimal(v.c_str(),out);
}

static std::string Fold(std::string v)
{
    for(size_t i=0;i<v.size();++i) if(v[i]>='A'&&v[i]<='Z') v[i]+='a'-'A';
    return v;
}

bool FoundryParseManifest(const char *json, FoundryManifest *out, std::string *error)
{
    if (!json || strlen(json)>1024*1024) { *error="Manifest too large"; return false; }
    cJSON *root=cJSON_Parse(json);
    bool ok=cJSON_IsObject(root);
    FoundryManifest m; m.totalBytes=0;
    const cJSON *version=cJSON_GetObjectItemCaseSensitive(root,"schema_version");
    ok=ok && cJSON_IsNumber(version) && version->valuedouble==1;
    m.id=Text(root,"artifact_id"); m.releaseId=Text(root,"release_id"); m.conversion=Text(root,"conversion");
    ok=ok && FoundryHex(m.id,32) && Text(root,"provider_id")=="foundry";
    std::set<std::string> paths, ids, componentIds, discs;
    if (ok && m.conversion=="server")
    {
        const cJSON *components=cJSON_GetObjectItemCaseSensitive(root,"components");
        ok=cJSON_IsArray(components) && cJSON_GetArraySize(components)>0 && cJSON_GetArraySize(components)<=100;
        unsigned fileCount=0;
        for(int i=0;ok && i<cJSON_GetArraySize(components);++i)
        {
            const cJSON *c=cJSON_GetArrayItem(components,i);
            FoundryComponent comp;
            comp.id=Text(c,"component_id"); comp.kind=Text(c,"kind"); comp.titleId=Text(c,"title_id");
            comp.mediaId=Text(c,"media_id"); comp.contentType=Text(c,"content_type");
            comp.contentId=Text(c,"content_id"); comp.baseVersion=Text(c,"base_version");
            const cJSON *dn=cJSON_GetObjectItemCaseSensitive(c,"disc_number"),*dc=cJSON_GetObjectItemCaseSensitive(c,"disc_count");
            if(dn){ok=ok&&cJSON_IsNumber(dn)&&dn->valuedouble>=0&&dn->valuedouble<=255&&dn->valuedouble==(int)dn->valuedouble;if(ok)comp.discNumber=(unsigned)dn->valueint;}
            if(dc){ok=ok&&cJSON_IsNumber(dc)&&dc->valuedouble>=0&&dc->valuedouble<=255&&dc->valuedouble==(int)dc->valuedouble;if(ok)comp.discCount=(unsigned)dc->valueint;}
            ok=ok && FoundryHex(comp.id,32) && FoundryHex(comp.titleId,8) && FoundryHex(comp.mediaId,8)
                && FoundryHex(comp.contentType,8) && FoundryHex(comp.contentId,40) && FoundryHex(comp.baseVersion,8);
            const char *expected=comp.kind=="god"?"00007000":comp.kind=="xbla"?"000D0000":comp.kind=="dlc"?"00000002":comp.kind=="title_update"?"000B0000":"";
            ok=ok && comp.contentType==expected;
            ok=ok && componentIds.insert(comp.id).second;
            if(comp.kind=="god")
            {
                std::string base=Fold(comp.titleId+"/"+comp.mediaId)+"/";
                char number[4]={(char)('0'+comp.discNumber/100),(char)('0'+comp.discNumber/10%10),(char)('0'+comp.discNumber%10),0};
                if(!comp.discNumber){for(std::set<std::string>::const_iterator it=discs.begin();it!=discs.end();++it)if(it->compare(0,base.size(),base)==0)ok=false;}
                else if(discs.count(base+"000"))ok=false;
                ok=ok && (!comp.discCount||comp.discNumber<=comp.discCount) && discs.insert(base+number).second;
            }
            const cJSON *files=cJSON_GetObjectItemCaseSensitive(c,"files");
            ok=ok && cJSON_IsArray(files) && cJSON_GetArraySize(files)>0;
            int headers=0; std::string header;
            for(int j=0;ok && j<cJSON_GetArraySize(files);++j)
            {
                const cJSON *f=cJSON_GetArrayItem(files,j); FoundryFile file;
                file.id=Text(f,"file_id"); file.root=Text(f,"root"); file.path=Text(f,"relative_path");
                file.role=Text(f,"role"); file.sha256=Fold(Text(f,"sha256"));
                std::string expectedUrl="/api/console/v1/artifacts/"+m.id+"/files/"+file.id;
                ok=++fileCount<=4096 && FoundryHex(file.id,32) && FoundryHex(file.sha256,64)
                    && FoundrySafePath(file.path) && Number(f,"bytes",&file.bytes) && file.bytes>0 && file.bytes<=0xEFFFFFFFULL
                    && Text(f,"download_path")==expectedUrl;
                if(file.root=="content") ok=ok && file.path.compare(0,18,comp.titleId+"/"+comp.contentType+"/")==0;
                else ok=ok && file.root=="cache" && comp.kind=="title_update" && file.path.find('/')==std::string::npos && file.path.compare(0,3,"TU_")==0;
                ok=ok && paths.insert(Fold(file.root+"/"+file.path)).second && ids.insert(file.id).second;
                if(comp.kind=="god")
                {
                    ok=ok && (file.role=="header" || file.role=="data") && file.root=="content";
                    if(file.role=="header") { ++headers; header=file.path; ok=ok && file.path.find('/',18)==std::string::npos; }
                }
                else ok=ok && file.role=="package" && cJSON_GetArraySize(files)==1
                    && (file.root=="cache" || file.path.find('/',18)==std::string::npos);
                m.totalBytes+=file.bytes; comp.files.push_back(file);
            }
            if(comp.kind=="god")
            {
                ok=ok && headers==1 && comp.files.size()>1;
                std::set<int> parts;
                for(size_t j=0;ok && j<comp.files.size();++j) if(comp.files[j].role=="data")
                {
                    const std::string &p=comp.files[j].path;
                    std::string prefix=header+".data/Data";
                    ok=p.compare(0,prefix.size(),prefix)==0 && p.size()==prefix.size()+4;
                    int n=0;
                    for(size_t k=prefix.size();ok && k<p.size();++k) { ok=p[k]>='0'&&p[k]<='9'; n=n*10+p[k]-'0'; }
                    if(ok) parts.insert(n);
                }
                for(size_t j=0;ok && j<parts.size();++j) ok=parts.count((int)j)==1;
            }
            m.components.push_back(comp);
        }
    }
    else if(ok && m.conversion=="console")
    {
        const cJSON *s=cJSON_GetObjectItemCaseSensitive(root,"source");
        FoundrySource &v=m.source; v.sha256=Fold(Text(s,"sha256")); v.member=Text(s,"member");
        v.name=Text(s,"name"); v.titleId=Text(s,"title_id");
        const cJSON *method=cJSON_GetObjectItemCaseSensitive(s,"method");
        std::string crc=Text(s,"crc32");
        ok=cJSON_IsNumber(method) && (method->valuedouble==0 || method->valuedouble==8);
        if(ok) v.method=method->valueint;
        ok=ok && Text(s,"format")=="zip" && FoundryHex(v.sha256,64) && FoundryHex(crc,8)
            && (v.titleId.empty() || FoundryHex(v.titleId,8)) && v.member.size()<=1024 && v.name.size()<=256
            && Number(s,"bytes",&v.bytes) && v.bytes>0 && v.bytes<=32ULL*1024*1024*1024
            && Number(s,"data_offset",&v.offset) && Number(s,"compressed_bytes",&v.packed)
            && Number(s,"image_bytes",&v.unpacked) && v.unpacked>0 && v.unpacked<=16ULL*1024*1024*1024
            && v.offset<=v.bytes && v.packed<=v.bytes-v.offset && (v.method!=0 || v.packed==v.unpacked)
            && Text(s,"download_path")=="/api/console/v1/source-artifacts/"+m.id+"/file";
        v.crc=strtoul(crc.c_str(),NULL,16); m.totalBytes=v.bytes;
    }
    else ok=false;
    cJSON_Delete(root);
    if(!ok) { *error="Invalid or unsupported Foundry manifest"; return false; }
    *out=m; return true;
}
