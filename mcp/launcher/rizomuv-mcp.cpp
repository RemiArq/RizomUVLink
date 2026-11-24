// rizomuv-mcp.exe -- fixed-path launcher of the RizomUV MCP server (Windows only).
//
// An MCP client (Claude Desktop, Cursor, VS Code, ...) starts a server as a plain
// executable and speaks JSON-RPC over its stdin/stdout. Node-based clients cannot start a
// .cmd/.bat without a shell, hence a real console .exe. This one sits at a path that does
// not change across yearly versions ({commonpf64}\Rizom Lab\mcp\rizomuv-mcp.exe), finds
// the newest RizomUV install, and runs that install's embedded Python on the server code
// the install ships:
//
//     "<app>\python.exe" -I -S -X utf8 "<app>\RizomUVLink\mcp\boot.py" <our own arguments>
//
// Contract -- keep it stable, an older launcher must keep working with a newer install
// and the other way round (the installer never downgrades this file):
//   - stdout belongs to the child, byte for byte. This program never writes to it.
//   - diagnostics go to stderr, one "rizomuv-mcp: " line each.
//   - the child runs in a job object with KILL_ON_JOB_CLOSE: when the client kills this
//     process (TerminateProcess is how MCP clients stop a server on Windows), the kernel
//     closes the job handle and the Python server dies with it, together with anything
//     it started that did not break away (CREATE_BREAKAWAY_FROM_JOB is allowed).
//   - the exit code is the child's; 3/4/5 are the launcher's own failures (see below).
//   - the only file layout relied upon inside an install: <app>\python.exe and
//     <app>\RizomUVLink\mcp\boot.py.
//
// Which install (first match wins):
//   1. RIZOMUV_MCP_APP_DIR   an install directory, used as is (error if unusable)
//   2. the directory of this exe, when it is itself an install (a copy pinned in {app})
//   3. HKLM\SOFTWARE\Rizom Lab\RizomUV VS RS <maj>.<min>\rizomuv.exe, default value
//      "{app}\rizomuv", highest <maj>.<min> first (compared as integers: 2027.10 beats
//      2027.2), 64-bit registry view before the 32-bit one; the first install whose
//      python.exe and boot script both exist -- so an older install without the MCP
//      server is skipped, not chosen. (RizomUVWinRegisterInstallPath in RizomUVLink.py
//      reads the same keys; it filters on a minimum version instead, having no file to
//      look for.)
// RIZOMUV_MCP_PYTHON and RIZOMUV_MCP_BOOT replace the two files individually (dev tree:
// python in RizomUVApp\bin, boot script in RizomUVLink\RizomUVLink\mcp).
// The child gets RIZOMUV_MCP_APP_DIR set to the install it runs from (when there is one),
// which is where the server finds rizomuv.exe to launch a headless instance, and
// RIZOMUV_MCP_LAUNCHER set to this exe.
//
// "rizomuv-mcp --launcher-info" prints the resolution to stderr and exits 0.
//
// Built by MakeMcpLauncher (RizomUVApp/makefiledist.inc.php) for every Windows config, or
// by build.cmd next to this file. Static CRT (/MT): the exe is installed outside {app},
// away from the VC++ redist dlls; it imports kernel32 and advapi32 only.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <algorithm>
#include <string>
#include <vector>

namespace {

const wchar_t kRegRoot[]   = L"SOFTWARE\\Rizom Lab";
const wchar_t kKeyPrefix[] = L"RizomUV VS RS ";        // + "<major>.<minor>"
const wchar_t kExeSubkey[] = L"rizomuv.exe";           // default value: "{app}\rizomuv"
const wchar_t kPythonRel[] = L"python.exe";
const wchar_t kBootRel[]   = L"RizomUVLink\\mcp\\boot.py";

enum : int {
    kExitNoInstall   = 3,   // nothing usable found
    kExitBadOverride = 4,   // an RIZOMUV_MCP_* variable points at nothing
    kExitSpawnFailed = 5,   // CreateProcess or the job object failed
};

// ---------------------------------------------------------------- stderr, never stdout

void Err(const std::wstring& msg)
{
    const std::wstring line = L"rizomuv-mcp: " + msg + L"\r\n";
    HANDLE h = GetStdHandle(STD_ERROR_HANDLE);
    if (h == nullptr || h == INVALID_HANDLE_VALUE)
        return;
    DWORD written = 0, mode = 0;
    if (GetConsoleMode(h, &mode)) {      // a real console: wide output renders any path
        WriteConsoleW(h, line.c_str(), static_cast<DWORD>(line.size()), &written, nullptr);
        return;
    }
    // a pipe or a file (what an MCP client gives us): UTF-8 bytes
    const int n = WideCharToMultiByte(CP_UTF8, 0, line.c_str(), static_cast<int>(line.size()),
                                      nullptr, 0, nullptr, nullptr);
    if (n <= 0)
        return;
    std::string utf8(static_cast<size_t>(n), '\0');
    WideCharToMultiByte(CP_UTF8, 0, line.c_str(), static_cast<int>(line.size()), &utf8[0], n,
                        nullptr, nullptr);
    WriteFile(h, utf8.data(), static_cast<DWORD>(utf8.size()), &written, nullptr);
}

std::wstring LastErrorText(DWORD code)
{
    wchar_t* buf = nullptr;
    FormatMessageW(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM |
                   FORMAT_MESSAGE_IGNORE_INSERTS, nullptr, code, 0,
                   reinterpret_cast<wchar_t*>(&buf), 0, nullptr);
    std::wstring text = buf ? buf : L"";
    if (buf)
        LocalFree(buf);
    while (!text.empty() && (text.back() == L'\n' || text.back() == L'\r' || text.back() == L' '))
        text.pop_back();
    return L"error " + std::to_wstring(code) + (text.empty() ? L"" : L" (" + text + L")");
}

// ---------------------------------------------------------------- small path helpers

std::wstring GetEnv(const wchar_t* name)
{
    DWORD n = GetEnvironmentVariableW(name, nullptr, 0);
    if (n == 0)
        return L"";
    std::wstring value(n, L'\0');
    n = GetEnvironmentVariableW(name, &value[0], n);
    value.resize(n);
    return value;
}

// The child inherits RIZOMUV_MCP_APP_DIR and the server resolves rizomuv.exe against it: a
// relative value would be read against whatever directory the server happens to be in.
std::wstring FullPath(const std::wstring& p)
{
    if (p.empty())
        return p;
    const DWORD n = GetFullPathNameW(p.c_str(), 0, nullptr, nullptr);
    if (n == 0)
        return p;
    std::wstring full(n, L'\0');
    const DWORD m = GetFullPathNameW(p.c_str(), n, &full[0], nullptr);
    if (m == 0 || m >= n)
        return p;
    full.resize(m);
    return full;
}

bool IsFile(const std::wstring& p)
{
    const DWORD a = GetFileAttributesW(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

std::wstring Join(std::wstring dir, const wchar_t* rel)
{
    if (!dir.empty() && dir.back() != L'\\' && dir.back() != L'/')
        dir += L'\\';
    return dir + rel;
}

std::wstring DirName(const std::wstring& p)
{
    const size_t i = p.find_last_of(L"\\/");
    return i == std::wstring::npos ? std::wstring() : p.substr(0, i);
}

std::wstring ModulePath()
{
    std::wstring buf(MAX_PATH, L'\0');
    for (;;) {
        const DWORD n = GetModuleFileNameW(nullptr, &buf[0], static_cast<DWORD>(buf.size()));
        if (n < buf.size()) {
            buf.resize(n);
            return buf;
        }
        buf.resize(buf.size() * 2);
    }
}

bool LooksLikeInstall(const std::wstring& dir)
{
    return !dir.empty() && IsFile(Join(dir, kPythonRel)) && IsFile(Join(dir, kBootRel));
}

// ---------------------------------------------------------------- registry

struct Version {
    bool ok;
    unsigned long major, minor;
};

// "2027.10" -> {2027, 10}. Digits, one dot, digits, at most 9 digits a side: anything
// else (a "RizomUV VS RS 2027.0 beta" key, say) is not ours and is ignored.
constexpr Version ParseVersion(const wchar_t* s, size_t n)
{
    Version v = {false, 0, 0};
    size_t dot = n;
    for (size_t i = 0; i < n; ++i) {
        if (s[i] == L'.') {
            if (dot != n)
                return v;
            dot = i;
        } else if (s[i] < L'0' || s[i] > L'9') {
            return v;
        }
    }
    if (dot == 0 || dot == n || dot + 1 == n || dot > 9 || n - dot - 1 > 9)
        return v;
    for (size_t i = 0; i < dot; ++i)
        v.major = v.major * 10 + static_cast<unsigned long>(s[i] - L'0');
    for (size_t i = dot + 1; i < n; ++i)
        v.minor = v.minor * 10 + static_cast<unsigned long>(s[i] - L'0');
    v.ok = true;
    return v;
}

constexpr bool Newer(const Version& a, const Version& b)
{
    return a.major != b.major ? a.major > b.major : a.minor > b.minor;
}

template <size_t N>
constexpr Version ParseLiteral(const wchar_t (&s)[N])
{
    return ParseVersion(s, N - 1);
}

// The ranking RizomUVWinRegisterInstallPath gets from re.fullmatch(r"([0-9]+)\.([0-9]+)")
// and a sort on (int, int): the launcher and the binding must pick the same install.
static_assert(ParseLiteral(L"2027.0").ok && ParseLiteral(L"2027.0").major == 2027 &&
              ParseLiteral(L"2027.0").minor == 0, "");
static_assert(ParseLiteral(L"2027.10").ok && ParseLiteral(L"2027.10").minor == 10, "");
static_assert(Newer(ParseLiteral(L"2027.10"), ParseLiteral(L"2027.2")),
              "the minor is compared as an integer, not as text");
static_assert(Newer(ParseLiteral(L"2028.0"), ParseLiteral(L"2027.99")), "");
static_assert(!Newer(ParseLiteral(L"2027.0"), ParseLiteral(L"2027.0")),
              "a strict ordering, as std::stable_sort requires");
static_assert(!ParseLiteral(L"").ok && !ParseLiteral(L"2027").ok && !ParseLiteral(L"2027.").ok &&
              !ParseLiteral(L".0").ok && !ParseLiteral(L"2027.0.1").ok &&
              !ParseLiteral(L"2027.0 beta").ok && !ParseLiteral(L" 2027.0").ok &&
              !ParseLiteral(L"2027.-1").ok && !ParseLiteral(L"1234567890.0").ok, "");

struct Install {
    Version version = {false, 0, 0};
    std::wstring key;      // "RizomUV VS RS 2027.0"
    std::wstring appDir;   // "C:\Program Files\Rizom Lab\RizomUV 2027.0"
    bool wow64_32 = false;
};

void CollectInstalls(bool wow64_32, std::vector<Install>& out)
{
    const REGSAM view = wow64_32 ? KEY_WOW64_32KEY : KEY_WOW64_64KEY;
    HKEY root = nullptr;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, kRegRoot, 0, KEY_READ | view, &root) != ERROR_SUCCESS)
        return;
    const size_t prefixLen = wcslen(kKeyPrefix);
    for (DWORD i = 0;; ++i) {
        wchar_t name[256];
        DWORD len = 256;
        const LONG rc = RegEnumKeyExW(root, i, name, &len, nullptr, nullptr, nullptr, nullptr);
        if (rc == ERROR_NO_MORE_ITEMS)
            break;
        if (rc != ERROR_SUCCESS)
            continue;                                   // e.g. a name longer than 255: not ours
        const std::wstring key(name, len);
        if (key.compare(0, prefixLen, kKeyPrefix) != 0)
            continue;
        Install inst;
        inst.version = ParseVersion(key.c_str() + prefixLen, key.size() - prefixLen);
        if (!inst.version.ok)
            continue;
        HKEY sub = nullptr;
        if (RegOpenKeyExW(root, (key + L"\\" + kExeSubkey).c_str(), 0, KEY_QUERY_VALUE | view, &sub) != ERROR_SUCCESS)
            continue;
        DWORD bytes = 0;
        std::wstring value;
        if (RegGetValueW(sub, nullptr, nullptr, RRF_RT_REG_SZ, nullptr, nullptr, &bytes) == ERROR_SUCCESS && bytes) {
            value.assign(bytes / sizeof(wchar_t) + 1, L'\0');
            if (RegGetValueW(sub, nullptr, nullptr, RRF_RT_REG_SZ, nullptr, &value[0], &bytes) == ERROR_SUCCESS)
                value.resize(wcslen(value.c_str()));
            else
                value.clear();
        }
        RegCloseKey(sub);
        if (value.empty())
            continue;
        inst.key = key;
        inst.appDir = DirName(value);                   // value is "{app}\rizomuv"
        inst.wow64_32 = wow64_32;
        out.push_back(inst);
    }
    RegCloseKey(root);
}

// ---------------------------------------------------------------- command line

// Everything after argv[0], verbatim: the child gets our arguments exactly as we got
// them, with no re-quoting. argv[0] follows the CRT rule: a quoted run or up to the
// first blank, no escapes (so no CommandLineToArgvW, and no shell32 dependency).
std::wstring ArgsAfterProgram(const wchar_t* p)
{
    if (*p == L'"') {
        ++p;
        while (*p && *p != L'"')
            ++p;
        if (*p)
            ++p;
    } else {
        while (*p && *p != L' ' && *p != L'\t')
            ++p;
    }
    while (*p == L' ' || *p == L'\t')
        ++p;
    return p;
}

std::wstring Quote(const std::wstring& path)   // a file path: no quote inside, no trailing '\'
{
    return L"\"" + path + L"\"";
}

BOOL WINAPI OnConsoleCtrl(DWORD type)
{
    // Ctrl+C / Ctrl+Break reach the child too (same console): let it decide, and keep
    // waiting for it. Close/logoff/shutdown: default handling, we exit, the job kills.
    return type == CTRL_C_EVENT || type == CTRL_BREAK_EVENT;
}

} // namespace

int wmain()
{
    const std::wstring args = ArgsAfterProgram(GetCommandLineW());
    const bool infoOnly = args.compare(0, 15, L"--launcher-info") == 0 &&
                          (args.size() == 15 || args[15] == L' ' || args[15] == L'\t');

    // ------------------------------------------------ resolve the install
    std::wstring appDir, source;
    std::vector<std::wstring> notes;

    const std::wstring envApp = FullPath(GetEnv(L"RIZOMUV_MCP_APP_DIR"));
    std::wstring python = FullPath(GetEnv(L"RIZOMUV_MCP_PYTHON"));
    std::wstring boot   = FullPath(GetEnv(L"RIZOMUV_MCP_BOOT"));

    if (!python.empty() && !IsFile(python)) {
        Err(L"RIZOMUV_MCP_PYTHON is set but is not a file: " + python);
        return kExitBadOverride;
    }
    if (!boot.empty() && !IsFile(boot)) {
        Err(L"RIZOMUV_MCP_BOOT is set but is not a file: " + boot);
        return kExitBadOverride;
    }

    if (!envApp.empty()) {
        // an explicit choice is never second-guessed: unusable is an error, not a fallback
        if ((python.empty() && !IsFile(Join(envApp, kPythonRel))) ||
            (boot.empty() && !IsFile(Join(envApp, kBootRel)))) {
            Err(L"RIZOMUV_MCP_APP_DIR is set but " + envApp + L" has no " +
                (python.empty() && !IsFile(Join(envApp, kPythonRel)) ? kPythonRel : kBootRel));
            return kExitBadOverride;
        }
        appDir = envApp;
        source = L"RIZOMUV_MCP_APP_DIR";
    } else if (python.empty() || boot.empty()) {
        const std::wstring selfDir = DirName(ModulePath());
        if (LooksLikeInstall(selfDir)) {
            appDir = selfDir;
            source = L"the launcher's own directory";
        } else {
            std::vector<Install> installs;
            CollectInstalls(false, installs);
            CollectInstalls(true, installs);
            // stable: at equal versions the 64-bit view, collected first, stays first
            std::stable_sort(installs.begin(), installs.end(), [](const Install& a, const Install& b) {
                return Newer(a.version, b.version);
            });
            for (const Install& inst : installs) {
                if (LooksLikeInstall(inst.appDir)) {
                    appDir = inst.appDir;
                    source = L"HKLM\\" + std::wstring(kRegRoot) + L"\\" + inst.key +
                             (inst.wow64_32 ? L" (32-bit view)" : L"");
                    break;
                }
                notes.push_back(L"skipped " + inst.key + L" at " + inst.appDir + L": no " +
                                (IsFile(Join(inst.appDir, kPythonRel)) ? kBootRel : kPythonRel) +
                                L" (an install older than the MCP server, or a broken one)");
            }
            if (installs.empty())
                notes.push_back(L"no \"" + std::wstring(kKeyPrefix) + L"<version>\" key under HKLM\\" + kRegRoot);
        }
    }

    if (python.empty() && !appDir.empty())
        python = Join(appDir, kPythonRel);
    if (boot.empty() && !appDir.empty())
        boot = Join(appDir, kBootRel);

    if (python.empty() || boot.empty()) {
        for (const std::wstring& n : notes)
            Err(n);
        Err(L"no RizomUV installation with the MCP server was found. Install RizomUV 2027.0 or "
            L"later, or set RIZOMUV_MCP_APP_DIR to an installation directory.");
        return kExitNoInstall;
    }

    const std::wstring cmd = Quote(python) + L" -I -S -X utf8 " + Quote(boot) +
                             (args.empty() || infoOnly ? L"" : L" " + args);
    if (infoOnly) {
        for (const std::wstring& n : notes)
            Err(n);
        Err(L"install  : " + (appDir.empty() ? std::wstring(L"(none, both files overridden)") : appDir) +
            (source.empty() ? L"" : L"   [from " + source + L"]"));
        Err(L"python   : " + python);
        Err(L"boot     : " + boot);
        Err(L"command  : " + cmd);
        return 0;
    }

    // ------------------------------------------------ spawn inside a kill-on-close job
    if (!appDir.empty())
        SetEnvironmentVariableW(L"RIZOMUV_MCP_APP_DIR", appDir.c_str());   // inherited by the child
    SetEnvironmentVariableW(L"RIZOMUV_MCP_LAUNCHER", ModulePath().c_str());

    HANDLE job = CreateJobObjectW(nullptr, nullptr);
    if (!job) {
        Err(L"CreateJobObject failed: " + LastErrorText(GetLastError()));
        return kExitSpawnFailed;
    }
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
                                              JOB_OBJECT_LIMIT_BREAKAWAY_OK;
    if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) {
        Err(L"SetInformationJobObject failed: " + LastErrorText(GetLastError()));
        return kExitSpawnFailed;
    }

    STARTUPINFOW si = {};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput  = GetStdHandle(STD_INPUT_HANDLE);
    si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    si.hStdError  = GetStdHandle(STD_ERROR_HANDLE);
    for (HANDLE h : {si.hStdInput, si.hStdOutput, si.hStdError})
        if (h && h != INVALID_HANDLE_VALUE)
            SetHandleInformation(h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);   // best effort

    DWORD flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT;
    // Started with no console at all (DETACHED_PROCESS): python.exe, a console program,
    // would otherwise get a brand new console -- a window popping up on the artist's desktop.
    if (GetConsoleCP() == 0)
        flags |= CREATE_NO_WINDOW;

    std::vector<wchar_t> cmdBuf(cmd.begin(), cmd.end());   // CreateProcessW may write into it
    cmdBuf.push_back(L'\0');
    PROCESS_INFORMATION pi = {};
    if (!CreateProcessW(python.c_str(), cmdBuf.data(), nullptr, nullptr, TRUE, flags, nullptr,
                        nullptr, &si, &pi)) {
        Err(L"cannot start " + python + L": " + LastErrorText(GetLastError()));
        return kExitSpawnFailed;
    }
    // Suspended until it is in the job, so nothing it starts can be born outside it.
    if (!AssignProcessToJobObject(job, pi.hProcess)) {
        // not fatal: the server still works, it just may outlive a killed launcher
        Err(L"warning: AssignProcessToJobObject failed, the server will not be killed with the "
            L"launcher: " + LastErrorText(GetLastError()));
    }
    SetConsoleCtrlHandler(OnConsoleCtrl, TRUE);
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);

    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(job);   // kills whatever the server left running inside the job
    return static_cast<int>(code);
}
