"""
obscam/libs.py
Win32 / D3D11 の型・定数・COM インターフェース定義
"""
import ctypes
import ctypes.wintypes as wintypes

# ─── HRESULT ──────────────────────────────────────────────────────────────────
S_OK = 0

# ─── D3D11 定数 ───────────────────────────────────────────────────────────────
D3D_DRIVER_TYPE_HARDWARE         = 1
D3D11_SDK_VERSION                = 7
D3D11_USAGE_STAGING              = 3
D3D11_CPU_ACCESS_READ            = 0x00020000
DXGI_FORMAT_B8G8R8A8_UNORM      = 87

D3D_FEATURE_LEVEL_11_0          = 0xB000
D3D_FEATURE_LEVEL_10_1          = 0xA100
D3D_FEATURE_LEVEL_10_0          = 0xA000

# ─── Win32 定数 ───────────────────────────────────────────────────────────────
INFINITE                         = 0xFFFFFFFF
WAIT_OBJECT_0                    = 0x00000000
WAIT_FAILED                      = 0xFFFFFFFF
FILE_MAP_ALL_ACCESS              = 0x000F001F
FILE_MAP_READ                    = 0x0004
FILE_MAP_WRITE                   = 0x0002
EVENT_MODIFY_STATE               = 0x0002
SYNCHRONIZE                      = 0x00100000
CREATE_NO_WINDOW                 = 0x08000000

# ─── D3D11 構造体 ─────────────────────────────────────────────────────────────
class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [
        ("Count",   wintypes.UINT),
        ("Quality", wintypes.UINT),
    ]

class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [
        ("Width",          wintypes.UINT),
        ("Height",         wintypes.UINT),
        ("MipLevels",      wintypes.UINT),
        ("ArraySize",      wintypes.UINT),
        ("Format",         wintypes.UINT),
        ("SampleDesc",     DXGI_SAMPLE_DESC),
        ("Usage",          wintypes.UINT),
        ("BindFlags",      wintypes.UINT),
        ("CPUAccessFlags", wintypes.UINT),
        ("MiscFlags",      wintypes.UINT),
    ]

class D3D11_BOX(ctypes.Structure):
    _fields_ = [
        ("left",   wintypes.UINT),
        ("top",    wintypes.UINT),
        ("front",  wintypes.UINT),
        ("right",  wintypes.UINT),
        ("bottom", wintypes.UINT),
        ("back",   wintypes.UINT),
    ]

class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [
        ("pData",      ctypes.c_void_p),
        ("RowPitch",   wintypes.UINT),
        ("DepthPitch", wintypes.UINT),
    ]

# ─── Win32 ヘルパー ───────────────────────────────────────────────────────────
_kernel32 = ctypes.windll.kernel32
_user32   = ctypes.windll.user32

def find_window(title: str) -> int:
    _user32.FindWindowW.restype = ctypes.c_void_p
    hwnd = _user32.FindWindowW(None, title)
    return hwnd or 0

def get_window_thread_process_id(hwnd: int):
    pid = wintypes.DWORD(0)
    tid = _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return tid, pid.value

def open_file_mapping(access: int, inherit: bool, name: str):
    _kernel32.OpenFileMappingW.restype = ctypes.c_void_p
    return _kernel32.OpenFileMappingW(access, inherit, name)

def map_view_of_file(handle, access, offset_hi, offset_lo, size):
    _kernel32.MapViewOfFile.restype = ctypes.c_void_p
    return _kernel32.MapViewOfFile(handle, access, offset_hi, offset_lo, size)

def unmap_view_of_file(ptr):
    if ptr:
        _kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        _kernel32.UnmapViewOfFile(ctypes.c_void_p(ptr))

def close_handle(handle):
    if handle:
        _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        _kernel32.CloseHandle(ctypes.c_void_p(handle))

def open_event(access: int, inherit: bool, name: str):
    _kernel32.OpenEventW.restype = ctypes.c_void_p
    return _kernel32.OpenEventW(access, inherit, name)

def set_event(handle) -> bool:
    _kernel32.SetEvent.argtypes = [ctypes.c_void_p]
    return bool(_kernel32.SetEvent(ctypes.c_void_p(handle) if handle else None))

def open_mutex(access: int, inherit: bool, name: str):
    _kernel32.OpenMutexW.restype = ctypes.c_void_p
    return _kernel32.OpenMutexW(access, inherit, name)

def create_mutex(name: str):
    _kernel32.CreateMutexW.restype = ctypes.c_void_p
    return _kernel32.CreateMutexW(None, False, name)

def wait_for_single_object(handle, timeout_ms: int = INFINITE) -> int:
    _kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    return _kernel32.WaitForSingleObject(ctypes.c_void_p(handle) if handle else None, timeout_ms)

def create_process(exe_path: str, cmd: str):
    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb",              wintypes.DWORD),
            ("lpReserved",      wintypes.LPWSTR),
            ("lpDesktop",       wintypes.LPWSTR),
            ("lpTitle",         wintypes.LPWSTR),
            ("dwX",             wintypes.DWORD),
            ("dwY",             wintypes.DWORD),
            ("dwXSize",         wintypes.DWORD),
            ("dwYSize",         wintypes.DWORD),
            ("dwXCountChars",   wintypes.DWORD),
            ("dwYCountChars",   wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags",         wintypes.DWORD),
            ("wShowWindow",     wintypes.WORD),
            ("cbReserved2",     wintypes.WORD),
            ("lpReserved2",     ctypes.c_char_p),
            ("hStdInput",       wintypes.HANDLE),
            ("hStdOutput",      wintypes.HANDLE),
            ("hStdError",       wintypes.HANDLE),
        ]
    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread",  wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId",  wintypes.DWORD),
        ]

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()
    buf = ctypes.create_unicode_buffer(cmd)
    _kernel32.SetLastError(0)
    # exe_path が None の場合は cmd の先頭トークンが使われる
    # exe_path を渡す場合は wchar_p にキャストする
    app = ctypes.c_wchar_p(exe_path) if exe_path else None
    ok = _kernel32.CreateProcessW(
        app,
        buf,
        None, None, False,
        CREATE_NO_WINDOW,
        None, None,
        ctypes.byref(si), ctypes.byref(pi),
    )
    if not ok:
        return None, None
    _kernel32.CloseHandle(pi.hThread)
    return pi.hProcess, pi.dwProcessId

def get_exit_code_process(handle) -> int:
    code = wintypes.DWORD(0)
    _kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    return code.value