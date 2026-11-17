"""Attach windowed CLI invocations to the calling console, preserving pipes."""
import ctypes
from ctypes import wintypes
import io
import os
import sys


def attach_cli_console():
    if sys.platform != 'win32': return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]; kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    kernel.GetFileType.argtypes = [wintypes.HANDLE]; kernel.GetFileType.restype = wintypes.DWORD
    kernel.AttachConsole.argtypes = [wintypes.DWORD]
    saved = {code: kernel.GetStdHandle(code & 0xffffffff) for code in (-11, -12)}
    kernel.AttachConsole(0xffffffff)
    for code, handle in saved.items():
        if handle and handle != ctypes.c_void_p(-1).value and kernel.GetFileType(handle) in (1, 3):
            kernel.SetStdHandle(code & 0xffffffff, handle)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    import msvcrt
    for name, code in (('stdout', -11), ('stderr', -12)):
        if getattr(sys, name) is not None: continue
        source = kernel.GetStdHandle(code & 0xffffffff)
        if not source or source == ctypes.c_void_p(-1).value: continue
        copied = wintypes.HANDLE(); process = kernel.GetCurrentProcess()
        if kernel.DuplicateHandle(process, source, process, ctypes.byref(copied), 0, False, 2):
            fd = msvcrt.open_osfhandle(copied.value, os.O_WRONLY | os.O_BINARY)
            setattr(sys, name, io.TextIOWrapper(os.fdopen(fd, 'wb', buffering=0), encoding='utf-8', write_through=True))


def install_cancel_handler(event):
    if sys.platform != 'win32': return lambda: None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def control(code):
        if code in (0, 1):
            event.set()
            return True
        return False
    callback = callback_type(control)
    kernel.SetConsoleCtrlHandler.argtypes = [callback_type, wintypes.BOOL]
    installed = bool(kernel.SetConsoleCtrlHandler(callback, True))
    def release():
        if installed: kernel.SetConsoleCtrlHandler(callback, False)
    return release
