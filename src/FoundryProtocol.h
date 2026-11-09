#ifndef FOUNDRY_PROTOCOL_H
#define FOUNDRY_PROTOCOL_H

// Portable protocol validation; also compiled by tools/test_foundry on a PC.
#include <string>
#include <vector>

struct FoundryFile
{
    std::string id, root, path, role, sha256;
    // Installer-only source override; never read from the server manifest.
    std::string installSource;
    unsigned long long bytes;
};

struct FoundryComponent
{
    std::string id, kind, titleId, mediaId, contentType, contentId, baseVersion;
    std::vector<FoundryFile> files;
    unsigned discNumber,discCount;
    bool avatarUnlock;
    FoundryComponent():discNumber(0),discCount(0),avatarUnlock(false){}
};

struct FoundrySource
{
    std::string sha256, member, name, titleId;
    unsigned long long bytes, offset, packed, unpacked;
    unsigned long crc;
    int method;
};

struct FoundryManifest
{
    std::string id, releaseId, conversion;
    std::vector<FoundryComponent> components;
    FoundrySource source;
    unsigned long long totalBytes;
};

bool FoundryHex(const std::string &value, unsigned count);
bool FoundryDecimal(const char *value, unsigned long long *out);
bool FoundrySafePath(const std::string &value);
bool FoundryOrigin(const std::string &value);
bool FoundryRetryableStatus(int status);
bool FoundryParseManifest(const char *json, FoundryManifest *out, std::string *error);

#endif
