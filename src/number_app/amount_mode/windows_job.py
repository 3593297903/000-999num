"""Parent-owned Windows job: kernel cleanup also covers child bootstrap time."""
import ctypes
from ctypes import wintypes

_KILL_ON_JOB_CLOSE = 0x00002000
_EXTENDED_LIMIT_INFORMATION = 9
_HANDLE_FLAG_INHERIT = 1


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class WindowsKillJob:
    """Only the audit parent owns the non-inheritable kernel job handle."""
    def __init__(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
        kernel.SetHandleInformation.restype = wintypes.BOOL
        kernel.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetHandleInformation.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        kernel.IsProcessInJob.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel = kernel
        self._handle = None
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(handle, _EXTENDED_LIMIT_INFORMATION,
                                                  ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.SetHandleInformation(handle, _HANDLE_FLAG_INHERIT, 0):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        self._handle = handle

    @property
    def inheritable(self):
        if self._handle is None:
            raise RuntimeError("Job句柄已关闭")
        flags = wintypes.DWORD()
        if not self._kernel.GetHandleInformation(self._handle, ctypes.byref(flags)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(flags.value & _HANDLE_FLAG_INHERIT)

    def assign(self, process_handle):
        if self._handle is None:
            raise RuntimeError("Job句柄已关闭，无法保护审查进程")
        if not self._kernel.AssignProcessToJobObject(self._handle, wintypes.HANDLE(process_handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.contains(process_handle):
            raise RuntimeError("审查进程未进入父协调者Job，停止审查")

    def contains(self, process_handle):
        if self._handle is None:
            raise RuntimeError("Job句柄已关闭")
        result = wintypes.BOOL()
        if not self._kernel.IsProcessInJob(wintypes.HANDLE(process_handle), self._handle, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(result.value)

    def close(self):
        if self._handle is not None:
            handle, self._handle = self._handle, None
            if not self._kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
