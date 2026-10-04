"""Windows verify 자식을 소유하는 Job Object. 다른 작업의 프로세스는 포함하지 않는다."""
import ctypes
import time
from ctypes import wintypes


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", ctypes.c_uint32), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
                ("process_limit", ctypes.c_uint32), ("affinity", ctypes.c_size_t),
                ("priority", ctypes.c_uint32), ("scheduling", ctypes.c_uint32)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6)] + [
        (name, ctypes.c_size_t) for name in ("process_memory", "job_memory", "peak_process", "peak_job")]


class Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")] + [
        (name, ctypes.c_uint32) for name in ("faults", "total", "active", "terminated")]


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel


def _check(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def process_identity(pid):
    kernel = _kernel()
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        _check(handle)
    try:
        waited = kernel.WaitForSingleObject(handle, 0)
        if waited == 0:
            return None
        if waited != 258:
            raise RuntimeError("Windows 자식 종료 관측 실패")
        times = [ctypes.c_ulonglong() for _ in range(4)]
        _check(kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)))
        return times[0].value
    finally:
        kernel.CloseHandle(handle)


class WindowsJob:
    def __init__(self, *, kernel=None):
        self.kernel = kernel or _kernel()
        self.handle = _check(self.kernel.CreateJobObjectW(None, None))
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            _check(self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def assign(self, process):
        _check(self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)))

    def contains(self, pid):
        handle = _check(self.kernel.OpenProcess(0x1000, False, pid))
        try:
            present = wintypes.BOOL()
            _check(self.kernel.IsProcessInJob(handle, self.handle, ctypes.byref(present)))
            return bool(present.value)
        finally:
            self.kernel.CloseHandle(handle)

    def active(self):
        accounting = Accounting()
        _check(self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None))
        return accounting.active

    def wait(self, timeout, *, remaining=0):
        deadline = time.monotonic() + timeout
        while self.active() > remaining:
            if time.monotonic() >= deadline:
                raise TimeoutError("Windows verify Job Object 자식 잔존")
            time.sleep(0.02)

    def terminate(self):
        _check(self.kernel.TerminateJobObject(self.handle, 1))

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
