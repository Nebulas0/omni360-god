#ifndef FOUNDRY_H
#define FOUNDRY_H

#include "DownloadQueue.h"
#include <string>
struct cJSON;

// Foundry is independent of the Archive.org catalog and credentials.
bool StartFoundry(const char *gamesRoot, const char *contentRoot);
void StopFoundry(DWORD timeoutMs);
void OpenFoundryUI(const char *gamesRoot, const char *contentRoot);
// The main Store uses the same visual renderers as the original provider.
bool OpenFoundryStoreUI(const char *gamesRoot, const char *contentRoot); // true: open Queue
bool OpenFoundryDlcUI(const char *gamesRoot, const char *contentRoot, const char *name, unsigned long titleId);
void OpenFoundryUpdatesUI(const char *gamesRoot, const char *contentRoot, unsigned long titleId);
bool FoundryStoreRequest(const std::string &path, cJSON **reply);
void FoundryInstallFromStore(const std::string &release, const std::string &name,
                            const char *gamesRoot, const char *contentRoot, bool digital, const std::string &forTitle=std::string());
int SnapshotFoundryJobs(QueueJobSnapshot *out, int maximum);
bool TakeFinishedFoundryJob(QueueJobSnapshot *out);
bool IsFoundryJobId(int id);
void CancelFoundryJob(int id);
void RemoveFoundryJob(int id);

#endif
