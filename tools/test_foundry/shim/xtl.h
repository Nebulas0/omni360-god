// Declaration-only syntax-check shim. This is NOT an Xbox SDK or a build.
#ifndef FOUNDRY_SYNTAX_XTL_H
#define FOUNDRY_SYNTAX_XTL_H
#define dprintf posix_dprintf
#include <stdio.h>
#undef dprintf
#include <stddef.h>
#include <stdint.h>
#include <wchar.h>
#define WINAPI
#define __cdecl
#include <stdarg.h>
int test_snprintf(char*,size_t,const char*,...);
#define _snprintf test_snprintf
typedef void *HANDLE;typedef void *LPVOID;typedef uint32_t DWORD;typedef int32_t LONG;
typedef int BOOL;typedef uint16_t WORD;typedef uint8_t BYTE;typedef wchar_t WCHAR;typedef const WCHAR *LPCWSTR;
struct CRITICAL_SECTION{int unused;};
struct ULARGE_INTEGER{unsigned long long QuadPart;};
struct WIN32_FIND_DATAA{DWORD dwFileAttributes;char cFileName[260];};
struct WIN32_FILE_ATTRIBUTE_DATA{DWORD dwFileAttributes,nFileSizeHigh,nFileSizeLow;};
#define TRUE 1
#define FALSE 0
#define INVALID_HANDLE_VALUE ((HANDLE)-1)
#define INVALID_FILE_ATTRIBUTES ((DWORD)-1)
#define FILE_ATTRIBUTE_DIRECTORY 16
#define FILE_ATTRIBUTE_NORMAL 128
#define GENERIC_WRITE 0x40000000
#define CREATE_ALWAYS 2
#define INFINITE ((DWORD)-1)
#define WAIT_OBJECT_0 0
#define WAIT_ABANDONED 128
#define CREATE_SUSPENDED 4
#define ERROR_SUCCESS 0
#define GetFileExInfoStandard 0
#define XINPUT_GAMEPAD_B 0x2000
extern "C" {
DWORD GetFileAttributesA(const char*);
BOOL CreateDirectoryA(const char*,void*);
HANDLE FindFirstFileA(const char*,WIN32_FIND_DATAA*);
BOOL FindNextFileA(HANDLE,WIN32_FIND_DATAA*);
BOOL FindClose(HANDLE);
BOOL RemoveDirectoryA(const char*);
BOOL DeleteFileA(const char*);
BOOL MoveFileA(const char*,const char*);
HANDLE CreateFileA(const char*,DWORD,DWORD,void*,DWORD,DWORD,HANDLE);
BOOL WriteFile(HANDLE,const void*,DWORD,DWORD*,void*);
BOOL FlushFileBuffers(HANDLE);
BOOL CloseHandle(HANDLE);
void InitializeCriticalSection(CRITICAL_SECTION*);
void DeleteCriticalSection(CRITICAL_SECTION*);
void EnterCriticalSection(CRITICAL_SECTION*);
void LeaveCriticalSection(CRITICAL_SECTION*);
LONG InterlockedExchange(volatile LONG*,LONG);
LONG InterlockedCompareExchange(volatile LONG*,LONG,LONG);
HANDLE CreateThread(void*,size_t,DWORD(WINAPI*)(LPVOID),LPVOID,DWORD,DWORD*);
HANDLE CreateEvent(void*,BOOL,BOOL,const char*);
HANDLE CreateMutexA(void*,BOOL,const char*);
BOOL ReleaseMutex(HANDLE);
BOOL SetEvent(HANDLE);
DWORD ResumeThread(HANDLE);
DWORD WaitForSingleObject(HANDLE,DWORD);
void Sleep(DWORD);
DWORD GetTickCount();
BOOL GetDiskFreeSpaceExA(const char*,ULARGE_INTEGER*,ULARGE_INTEGER*,ULARGE_INTEGER*);
BOOL GetFileAttributesExA(const char*,int,WIN32_FILE_ATTRIBUTE_DATA*);
}
#endif
