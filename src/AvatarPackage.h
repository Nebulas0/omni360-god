#ifndef AVATAR_PACKAGE_H
#define AVATAR_PACKAGE_H
#include <string>
#include <vector>
bool AvatarOnlineXuid(unsigned long long xuid);
bool AvatarPatchHeader(std::vector<unsigned char> *header,
    const std::vector<unsigned long long> &xuids, std::string *relativePath,
    std::string *contentId, std::string *error);
#endif
