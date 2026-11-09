#ifndef FOUNDRY_H
#define FOUNDRY_H

#include "DownloadQueue.h"
#include <string>
#include <vector>
struct cJSON;

#define MAX_FOUNDRY_JOBS 65

// Foundry is independent of the Archive.org catalog and credentials.
bool StartFoundry(const char *gamesRoot, const char *contentRoot);
void StopFoundry(DWORD timeoutMs);
void SetFoundryInstallPaths(const char *gamesRoot, const char *contentRoot);
void OpenFoundryUI(const char *gamesRoot, const char *contentRoot);
// The main Store uses the same visual renderers as the original provider.
bool OpenFoundryStoreUI(const char *gamesRoot, const char *contentRoot); // true: open Queue
bool OpenFoundryPreparedUI(const char *gamesRoot, const char *contentRoot);
bool FoundryQueuePrepared(const cJSON *reply, const char *gamesRoot, const char *contentRoot, const std::string &only=std::string());
bool FoundryPreparedCompleted(const std::string &requestId);
void FoundryPreparedOptionsUI();
bool FoundryCanUnlockAvatars();
bool OpenFoundryDlcUI(const char *gamesRoot, const char *contentRoot, const char *name, unsigned long titleId);
void OpenFoundryUpdatesUI(const char *gamesRoot, const char *contentRoot, unsigned long titleId);
void FoundryInstallDlcBatch(const std::vector<std::string> &releases, const std::string &name,
                           const char *gamesRoot, const char *contentRoot, const std::string &forTitle);
void FoundryInstallDiscSet(const std::vector<std::string> &releases, const std::string &name,
                          const char *gamesRoot, const char *contentRoot, bool contentPackages);
bool FoundryStoreRequest(const std::string &path, cJSON **reply);
void PrefetchFoundryStore(const std::string &path);
void ClearFoundryStoreCache();
void FoundryInstallFromStore(const std::string &release, const std::string &name,
                            const char *gamesRoot, const char *contentRoot, bool digital, const std::string &forTitle=std::string());
int SnapshotFoundryActiveJob(QueueJobSnapshot *out);
int SnapshotFoundryJobs(QueueJobSnapshot *out, int maximum);
bool TakeFinishedFoundryJob(QueueJobSnapshot *out);
bool IsFoundryJobId(int id);
void CancelFoundryJob(int id);
void RemoveFoundryJob(int id);

#endif
