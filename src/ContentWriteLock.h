#ifndef CONTENT_WRITE_LOCK_H
#define CONTENT_WRITE_LOCK_H

// Serialize commits with upstream disc/game/DLC/TU writers. A recursive named
// mutex also covers nested GoD cleanup, without keeping a lock in UI code.
#ifdef _XBOX
#include <xtl.h>
class ContentWriteLock
{
    HANDLE handle;
    bool held;
public:
    ContentWriteLock():handle(CreateMutexA(NULL,FALSE,"Omni360GoDContentWrite")),held(false)
    {
        if(handle) { DWORD status=WaitForSingleObject(handle,INFINITE);held=status==WAIT_OBJECT_0||status==WAIT_ABANDONED; }
    }
    ~ContentWriteLock(){if(held)ReleaseMutex(handle);if(handle)CloseHandle(handle);}
    bool Ok()const{return held;}
private:
    ContentWriteLock(const ContentWriteLock&);
    ContentWriteLock&operator=(const ContentWriteLock&);
};
#else
class ContentWriteLock{public:bool Ok()const{return true;}};
#endif
#endif
