#ifndef FOUNDRY_H
#define FOUNDRY_H

#include "DownloadQueue.h"

// Foundry is independent of the Archive.org catalog and credentials.
bool StartFoundry(const char *gamesRoot, const char *contentRoot);
void StopFoundry(DWORD timeoutMs);
void OpenFoundryUI(const char *gamesRoot, const char *contentRoot);
int SnapshotFoundryJobs(QueueJobSnapshot *out, int maximum);
bool TakeFinishedFoundryJob(QueueJobSnapshot *out);
bool IsFoundryJobId(int id);
void CancelFoundryJob(int id);
void RemoveFoundryJob(int id);

#endif
