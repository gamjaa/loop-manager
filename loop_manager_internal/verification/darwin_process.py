"""macOS libproc로 PID 재사용을 구분하고 부모 PID를 확인한다."""
import ctypes
import errno
import os


class ProcessInfo(ctypes.Structure):
    # XNU bsd/sys/proc_info.h의 PROC_PIDTBSDINFO ABI.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "exit_status", "pid", "ppid", "uid", "gid",
        "ruid", "rgid", "svuid", "svgid", "reserved",
    )] + [("command", ctypes.c_char * 16), ("name", ctypes.c_char * 32)] + [
        (name, ctypes.c_uint32) for name in ("files", "pgid", "job", "device", "tty_group")
    ] + [("nice", ctypes.c_int32), ("seconds", ctypes.c_uint64), ("microseconds", ctypes.c_uint64)]


def process_info(pid):
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    query = library.proc_pidinfo
    query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    query.restype = ctypes.c_int
    info = ProcessInfo()
    ctypes.set_errno(0)
    size = query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if size == 0 and ctypes.get_errno() == errno.ESRCH:
        return None
    if size != ctypes.sizeof(info):
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error))
    if info.pid != pid or not info.seconds:
        raise RuntimeError("macOS 프로세스 identity 해석 실패")
    if info.status == 5:  # SZOMB
        return None
    return info.ppid, info.seconds * 1_000_000 + info.microseconds
