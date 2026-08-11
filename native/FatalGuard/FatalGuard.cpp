/*
 * FatalGuard 3.1.0-beta.1 — BFBB-safe exit swallow + mid-game MCP pump kick.
 * SemVer 2.0.0 (https://semver.org/). beta: usable, not a stability lock.
 *
 * Why earlier builds crashed on boot:
 *   - IAT-patched EVERY loaded module (d3d9, XInput, etc.) → immediate AVs
 *   - MessageBox/TerminateProcess hooks on system DLLs
 *   - Broken UE4SS start_mod() export (void return / wrong ABI)
 *
 * Current rules:
 *   - Hook ONLY the game main module (GetModuleHandle null)
 *   - ONLY ExitProcess + abort (optional MessageBox on main exe only)
 *   - Delay hooks 45s by default (after DXGI/UE4SS settle)
 *   - process_alive.json writer (no UE APIs)
 *   - Valid start_mod / uninstall_mod stubs for UE4SS C++ loader
 *   - Mid-game revive: AliveThread watches revive.flag + heartbeat.json,
 *     synthetic Ctrl+F9 via SendInput → Lua force_revive + immediate pump
 */

#define FATALGUARD_VERSION_SEMVER "3.1.0-beta.1"
/* Compact int for simple consumers: MAJOR*100 + MINOR*10 + PATCH = 310 */
#define FATALGUARD_VERSION_CODE 310

#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <atomic>

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------
static CRITICAL_SECTION g_cs;
static bool g_csInit = false;
static wchar_t g_logPath[MAX_PATH];
static wchar_t g_ipcDir[MAX_PATH];
static wchar_t g_alivePath[MAX_PATH];

static void EnsureCs()
{
    if (!g_csInit)
    {
        InitializeCriticalSection(&g_cs);
        g_csInit = true;
    }
}

static void PathsInit()
{
    static bool once = false;
    if (once)
        return;
    once = true;
    EnsureCs();
    GetModuleFileNameW(nullptr, g_logPath, MAX_PATH);
    wchar_t* slash = wcsrchr(g_logPath, L'\\');
    if (slash)
        *(slash + 1) = 0;
    wcscpy_s(g_ipcDir, g_logPath);
    wcscat_s(g_ipcDir, L"UnrealEngineMCP_IPC\\");
    wcscpy_s(g_alivePath, g_ipcDir);
    wcscat_s(g_alivePath, L"process_alive.json");
    wcscat_s(g_logPath, L"FatalGuard.log");
    CreateDirectoryW(g_ipcDir, nullptr);
}

static void Log(const char* msg)
{
    PathsInit();
    EnterCriticalSection(&g_cs);
    FILE* f = nullptr;
    _wfopen_s(&f, g_logPath, L"a");
    if (f)
    {
        SYSTEMTIME st;
        GetLocalTime(&st);
        fprintf(f, "%04d-%02d-%02d %02d:%02d:%02d.%03d %s\n",
                st.wYear, st.wMonth, st.wDay,
                st.wHour, st.wMinute, st.wSecond, st.wMilliseconds, msg);
        fclose(f);
    }
    LeaveCriticalSection(&g_cs);
    OutputDebugStringA("[FatalGuard] ");
    OutputDebugStringA(msg);
    OutputDebugStringA("\n");
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
// FATALGUARD=0          disable
// FATALGUARD_DELAY_MS   default 45000
// FATALGUARD_MSGBOX=1   also hook MessageBoxW on main exe only (default 0)
// FATALGUARD_ABORT=0    skip abort hook (default 1 = hook)
static bool g_enabled = true;
static bool g_hookMsgBox = false;
static bool g_hookAbort = true;
static DWORD g_delayMs = 45000;
static std::atomic<int> g_swallowed{0};
static std::atomic<int> g_aliveTicks{0};
static std::atomic<bool> g_hooksOn{false};

static void ReadConfig()
{
    char buf[32];
    if (GetEnvironmentVariableA("FATALGUARD", buf, sizeof(buf)) > 0 && buf[0] == '0')
        g_enabled = false;
    if (GetEnvironmentVariableA("FATALGUARD_MSGBOX", buf, sizeof(buf)) > 0 && buf[0] == '1')
        g_hookMsgBox = true;
    if (GetEnvironmentVariableA("FATALGUARD_ABORT", buf, sizeof(buf)) > 0 && buf[0] == '0')
        g_hookAbort = false;
    if (GetEnvironmentVariableA("FATALGUARD_DELAY_MS", buf, sizeof(buf)) > 0)
    {
        int v = atoi(buf);
        if (v >= 0 && v <= 300000)
            g_delayMs = (DWORD)v;
    }
}

// ---------------------------------------------------------------------------
// IAT patch — MAIN MODULE ONLY
// ---------------------------------------------------------------------------
static bool PatchIATOne(HMODULE module, const char* importDll, const char* funcName, void* hook, void** outOriginal)
{
    if (!module)
        return false;
    auto* dos = (PIMAGE_DOS_HEADER)module;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE)
        return false;
    auto* nt = (PIMAGE_NT_HEADERS)((BYTE*)module + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE)
        return false;
    auto& dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir.VirtualAddress || !dir.Size)
        return false;

    auto* imp = (PIMAGE_IMPORT_DESCRIPTOR)((BYTE*)module + dir.VirtualAddress);
    for (; imp->Name; ++imp)
    {
        const char* dllName = (const char*)((BYTE*)module + imp->Name);
        if (_stricmp(dllName, importDll) != 0)
            continue;

        auto* thunk = (PIMAGE_THUNK_DATA)((BYTE*)module + imp->FirstThunk);
        auto* orig = imp->OriginalFirstThunk
            ? (PIMAGE_THUNK_DATA)((BYTE*)module + imp->OriginalFirstThunk)
            : thunk;

        for (; orig->u1.AddressOfData; ++orig, ++thunk)
        {
            if (IMAGE_SNAP_BY_ORDINAL(orig->u1.Ordinal))
                continue;
            auto* ibn = (PIMAGE_IMPORT_BY_NAME)((BYTE*)module + orig->u1.AddressOfData);
            if (!ibn || strcmp(ibn->Name, funcName) != 0)
                continue;

            DWORD oldProt = 0;
            if (!VirtualProtect(&thunk->u1.Function, sizeof(void*), PAGE_READWRITE, &oldProt))
                return false;
            if (outOriginal)
                *outOriginal = (void*)(uintptr_t)thunk->u1.Function;
            thunk->u1.Function = (ULONG_PTR)hook;
            VirtualProtect(&thunk->u1.Function, sizeof(void*), oldProt, &oldProt);
            return true;
        }
    }
    return false;
}

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------
using ExitProcess_t = void(WINAPI*)(UINT);
using Abort_t = void(*)();
using MessageBoxW_t = int(WINAPI*)(HWND, LPCWSTR, LPCWSTR, UINT);

static ExitProcess_t RealExitProcess = nullptr;
static Abort_t RealAbort = nullptr;
static MessageBoxW_t RealMessageBoxW = nullptr;

static void WINAPI HookExitProcess(UINT code)
{
    char msg[96];
    sprintf_s(msg, "SWALLOWED ExitProcess(%u) n=%d", code, g_swallowed.load() + 1);
    Log(msg);
    g_swallowed.fetch_add(1);
    // Intentionally do not exit.
}

static void HookAbort()
{
    Log("SWALLOWED abort()");
    g_swallowed.fetch_add(1);
}

static bool LooksFatalW(const wchar_t* caption, const wchar_t* text)
{
    auto has = [](const wchar_t* s, const wchar_t* sub) -> bool {
        return s && sub && wcsstr(s, sub) != nullptr;
    };
    return has(caption, L"Fatal") || has(text, L"Fatal") || has(text, L"FATAL")
        || has(text, L"Assertion failed") || has(text, L"Ensure condition")
        || has(text, L"DXGI_ERROR");
}

static int WINAPI HookMessageBoxW(HWND hWnd, LPCWSTR text, LPCWSTR caption, UINT type)
{
    if (LooksFatalW(caption, text))
    {
        Log("SWALLOWED MessageBoxW (fatal-style)");
        g_swallowed.fetch_add(1);
        if (type & MB_YESNO)
            return IDYES;
        return IDOK;
    }
    if (RealMessageBoxW)
        return RealMessageBoxW(hWnd, text, caption, type);
    return IDOK;
}

static void InstallHooks()
{
    if (g_hooksOn.exchange(true))
        return;
    ReadConfig();
    if (!g_enabled)
    {
        Log("FATALGUARD=0 — skip hooks");
        return;
    }

    HMODULE mainMod = GetModuleHandleW(nullptr);
    if (!mainMod)
    {
        Log("no main module");
        return;
    }

    Log("v3 installing MAIN-EXE-ONLY hooks");

    if (PatchIATOne(mainMod, "KERNEL32.dll", "ExitProcess", (void*)&HookExitProcess, (void**)&RealExitProcess))
        Log("hooked ExitProcess on main exe");
    else
        Log("WARN: ExitProcess IAT not found on main exe");

    // Some UE builds import via api-ms-*
    if (!RealExitProcess)
    {
        if (PatchIATOne(mainMod, "api-ms-win-core-processthreads-l1-1-0.dll", "ExitProcess",
                        (void*)&HookExitProcess, (void**)&RealExitProcess))
            Log("hooked ExitProcess via api-ms on main exe");
    }

    if (g_hookAbort)
    {
        if (PatchIATOne(mainMod, "ucrtbase.dll", "abort", (void*)&HookAbort, (void**)&RealAbort))
            Log("hooked abort (ucrtbase) on main exe");
        else if (PatchIATOne(mainMod, "msvcrt.dll", "abort", (void*)&HookAbort, (void**)&RealAbort))
            Log("hooked abort (msvcrt) on main exe");
        else
            Log("abort IAT not present on main exe (ok)");
    }

    if (g_hookMsgBox)
    {
        if (PatchIATOne(mainMod, "USER32.dll", "MessageBoxW", (void*)&HookMessageBoxW, (void**)&RealMessageBoxW))
            Log("hooked MessageBoxW on main exe");
        else
            Log("MessageBoxW IAT not on main exe (ok)");
    }

    Log("v3 hooks ready (main exe only)");
}

// ---------------------------------------------------------------------------
// Mid-game pump kick (synthetic Ctrl+F9 → UnrealEngineMCP force_revive)
// ---------------------------------------------------------------------------
// FATALGUARD_AUTOKICK=0  disable auto kick
// Heartbeat older than this (seconds) triggers kick even without revive.flag
static bool g_autoKick = true;
// Allow multi-second MCP commands without thrashing force_revive.
// Soft-dead pump still recovers within ~10s; avoids deleting mid-request races.
static const DWORD HEARTBEAT_STALE_SEC = 10;
static const DWORD KICK_COOLDOWN_MS = 2000;
static DWORD g_lastKickMs = 0;
static std::atomic<int> g_kicks{0};

static void ReadAutoKickConfig()
{
    char buf[32];
    if (GetEnvironmentVariableA("FATALGUARD_AUTOKICK", buf, sizeof(buf)) > 0 && buf[0] == '0')
        g_autoKick = false;
}

struct EnumFindCtx
{
    DWORD pid;
    HWND hwnd;
};

static BOOL CALLBACK EnumWindowsFindPid(HWND hwnd, LPARAM lp)
{
    auto* ctx = (EnumFindCtx*)lp;
    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);
    if (pid != ctx->pid)
        return TRUE;
    if (!IsWindowVisible(hwnd))
        return TRUE;
    // Prefer a real game window (has size)
    RECT rc{};
    if (!GetWindowRect(hwnd, &rc))
        return TRUE;
    if ((rc.right - rc.left) < 100 || (rc.bottom - rc.top) < 100)
        return TRUE;
    ctx->hwnd = hwnd;
    return FALSE; // stop
}

static HWND FindGameHwnd()
{
    EnumFindCtx ctx{ GetCurrentProcessId(), nullptr };
    EnumWindows(EnumWindowsFindPid, (LPARAM)&ctx);
    return ctx.hwnd;
}

static bool HeartbeatStale(DWORD staleSec)
{
    wchar_t hbPath[MAX_PATH];
    wcscpy_s(hbPath, g_ipcDir);
    wcscat_s(hbPath, L"heartbeat.json");
    WIN32_FILE_ATTRIBUTE_DATA fad{};
    if (!GetFileAttributesExW(hbPath, GetFileExInfoStandard, &fad))
        return true; // missing = treat as dead pump
    FILETIME ft = fad.ftLastWriteTime;
    ULARGE_INTEGER uli{};
    uli.LowPart = ft.dwLowDateTime;
    uli.HighPart = ft.dwHighDateTime;
    FILETIME nowFt{};
    GetSystemTimeAsFileTime(&nowFt);
    ULARGE_INTEGER now{};
    now.LowPart = nowFt.dwLowDateTime;
    now.HighPart = nowFt.dwHighDateTime;
    // 100ns units → seconds
    unsigned long long ageSec = (now.QuadPart - uli.QuadPart) / 10000000ULL;
    return ageSec >= staleSec;
}

static bool ReviveFlagPresent()
{
    wchar_t path[MAX_PATH];
    wcscpy_s(path, g_ipcDir);
    wcscat_s(path, L"revive.flag");
    DWORD attr = GetFileAttributesW(path);
    return attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY);
}

// Send Ctrl+F9 so UnrealEngineMCP's RegisterKeyBind runs force_revive.
// Win32Async input (BFBB UE4SS) observes SendInput when the process receives it.
static bool KickPumpViaCtrlF9(const char* why)
{
    DWORD now = GetTickCount();
    if (g_lastKickMs != 0 && (now - g_lastKickMs) < KICK_COOLDOWN_MS)
        return false;
    g_lastKickMs = now;

    HWND hwnd = FindGameHwnd();
    if (hwnd)
    {
        // Best-effort focus so Win32Async/keybinds see the chord
        if (GetForegroundWindow() != hwnd)
        {
            // Allow SetForegroundWindow from background thread (best effort)
            DWORD fgTid = GetWindowThreadProcessId(GetForegroundWindow(), nullptr);
            DWORD ourTid = GetCurrentThreadId();
            if (fgTid && fgTid != ourTid)
            {
                AttachThreadInput(ourTid, fgTid, TRUE);
                SetForegroundWindow(hwnd);
                AttachThreadInput(ourTid, fgTid, FALSE);
            }
            else
            {
                SetForegroundWindow(hwnd);
            }
        }
    }

    INPUT inputs[6] = {};
    // Ctrl down
    inputs[0].type = INPUT_KEYBOARD;
    inputs[0].ki.wVk = VK_CONTROL;
    // F9 down
    inputs[1].type = INPUT_KEYBOARD;
    inputs[1].ki.wVk = VK_F9;
    // F9 up
    inputs[2].type = INPUT_KEYBOARD;
    inputs[2].ki.wVk = VK_F9;
    inputs[2].ki.dwFlags = KEYEVENTF_KEYUP;
    // Ctrl up
    inputs[3].type = INPUT_KEYBOARD;
    inputs[3].ki.wVk = VK_CONTROL;
    inputs[3].ki.dwFlags = KEYEVENTF_KEYUP;

    UINT sent = SendInput(4, inputs, sizeof(INPUT));
    int n = g_kicks.fetch_add(1) + 1;
    char msg[160];
    sprintf_s(msg, "auto-kick Ctrl+F9 n=%d sent=%u why=%s hwnd=%p",
              n, (unsigned)sent, why ? why : "?", (void*)hwnd);
    Log(msg);
    return sent == 4;
}

static void MaybeAutoKickPump()
{
    if (!g_autoKick)
        return;
    const bool flag = ReviveFlagPresent();
    const bool stale = HeartbeatStale(HEARTBEAT_STALE_SEC);
    if (!flag && !stale)
        return;
    KickPumpViaCtrlF9(flag ? "revive.flag" : "heartbeat_stale");
}

// ---------------------------------------------------------------------------
// Threads
// ---------------------------------------------------------------------------
static DWORD WINAPI AliveThread(LPVOID)
{
    PathsInit();
    ReadAutoKickConfig();
    Log(g_autoKick ? "AliveThread + auto-kick ON (revive.flag / stale heartbeat)"
                   : "AliveThread auto-kick OFF (FATALGUARD_AUTOKICK=0)");
    while (true)
    {
        int n = g_aliveTicks.fetch_add(1) + 1;
        FILE* f = nullptr;
        _wfopen_s(&f, g_alivePath, L"wb");
        if (f)
        {
            fprintf(f,
                    "{\"ok\":true,\"process_alive\":true,\"ticks\":%d,\"swallowed\":%d,"
                    "\"hooks\":%s,\"version\":%d,\"version_semver\":\"%s\","
                    "\"kicks\":%d,\"autokick\":%s,\"ts\":%llu}\n",
                    n, g_swallowed.load(), g_hooksOn.load() ? "true" : "false",
                    FATALGUARD_VERSION_CODE, FATALGUARD_VERSION_SEMVER,
                    g_kicks.load(), g_autoKick ? "true" : "false",
                    (unsigned long long)time(nullptr));
            fclose(f);
        }

        // Mid-game revive: kick Lua keybind when MCP writes revive.flag or pump dies.
        MaybeAutoKickPump();

        // Faster poll when recovering so revive feels snappy
        const bool recovering = g_autoKick && (ReviveFlagPresent() || HeartbeatStale(HEARTBEAT_STALE_SEC));
        Sleep(recovering ? 400 : 1000);
    }
    return 0;
}

static DWORD WINAPI DelayedHookThread(LPVOID)
{
    ReadConfig();
    if (!g_enabled)
    {
        Log("disabled — no delayed hooks");
        return 0;
    }
    char msg[80];
    sprintf_s(msg, "v3 waiting %u ms before main-exe hooks", (unsigned)g_delayMs);
    Log(msg);
    Sleep(g_delayMs);
    InstallHooks();
    return 0;
}

// ---------------------------------------------------------------------------
// UE4SS C++ mod exports (opaque stub — no UE4SS SDK link)
// Loader calls start_mod() and may invoke virtuals on the pointer periodically.
// We provide a no-op vtable + zeroed storage so it does not AV.
// ---------------------------------------------------------------------------
using VFn = void(__fastcall*)(void* self);

static void __fastcall VNoOp(void*)
{
}

// ~32 slots covers destructor + virtuals on current CppUserModBase
static VFn g_vtbl[40];
static bool g_vtblInit = false;

static void InitVtbl()
{
    if (g_vtblInit)
        return;
    for (int i = 0; i < 40; ++i)
        g_vtbl[i] = &VNoOp;
    g_vtblInit = true;
}

struct OpaqueMod
{
    void** vptr;
    unsigned char pad[2048]; // room for StringType members if ever read
};

extern "C" __declspec(dllexport) void* start_mod()
{
    InitVtbl();
    PathsInit();
    Log("start_mod() — UE4SS C++ mod entry (opaque stub)");
    auto* m = (OpaqueMod*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(OpaqueMod));
    if (!m)
        return nullptr;
    m->vptr = (void**)g_vtbl;
    return m;
}

extern "C" __declspec(dllexport) void uninstall_mod(void* mod)
{
    Log("uninstall_mod()");
    if (mod)
        HeapFree(GetProcessHeap(), 0, mod);
}

// ---------------------------------------------------------------------------
// DllMain — only start threads, never hook here
// ---------------------------------------------------------------------------
BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH)
    {
        DisableThreadLibraryCalls(hModule);
        PathsInit();
        Log("FatalGuard " FATALGUARD_VERSION_SEMVER " loaded (main-exe hooks delayed; process_alive + auto-kick)");
        CreateThread(nullptr, 0, AliveThread, nullptr, 0, nullptr);
        CreateThread(nullptr, 0, DelayedHookThread, nullptr, 0, nullptr);
    }
    return TRUE;
}
