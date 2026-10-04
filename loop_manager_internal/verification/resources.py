"""운영체제 관측과 모델을 호출하지 않는 추가 실행량 계산."""
from __future__ import annotations
import ctypes
from dataclasses import dataclass
import math
import os
from pathlib import Path
import time

MIB = 1024 * 1024
DEFAULT_WORKER_BYTES = 128 * MIB  # 실측 전 초기 보호값이며 실측 RSS로 상향한다.
SNAPSHOT_MAX_AGE = 2.0
RESERVATION_GRACE = 0.75


def parse_parallelism(value: str) -> str | int:
    value = value.strip()
    if value == "auto":
        return value
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError("verify_parallelism은 auto 또는 양의 정수여야 합니다.")
    return int(value)


@dataclass(frozen=True)
class Snapshot:
    at: float
    cpus: float
    busy: float | None
    available: int | None
    source: str
    reason: str = ""
    affinity_cpus: int | None = None
    applied_quota: float | None = None


@dataclass(frozen=True)
class Decision:
    granted: bool
    reason: str
    limit: int
    fallback: bool = False


def decide(snapshot: Snapshot, *, now: float, running: int, cap: str | int,
           pending_cpu: float = 0, pending_memory: int = 0,
           worker_bytes: int = DEFAULT_WORKER_BYTES, existing: bool = False,
           global_running: int = 0) -> Decision:
    """고정 분할 없이 한 추가 모듈의 배정을 판단한다."""
    limit = max(1, math.ceil(snapshot.cpus))
    if cap != "auto":
        limit = min(limit, cap)
    if running >= limit:
        return Decision(False, "사용자/CPU 상한", limit)
    memory_cost = 0 if existing else worker_bytes
    if snapshot.available is not None:
        reserve = min(256 * MIB, max(64 * MIB, snapshot.available // 10))
        if snapshot.available - reserve - pending_memory < memory_cost:
            return Decision(False, "메모리 보호 여유 부족", limit)
    stale = now < snapshot.at or now - snapshot.at > SNAPSHOT_MAX_AGE
    if stale or snapshot.busy is None or snapshot.available is None:
        allowed = running == 0 and global_running == 0 and pending_cpu == 0
        return Decision(allowed, "관측 실패/낡은 관측: 직렬 축소", 1, True)
    free = snapshot.cpus - snapshot.busy - pending_cpu
    # 확대에는 CPU 한 개의 여유를 요구하고 단일 CPU의 진행은 더 낮은 문턱을 쓴다.
    threshold = min(1.0, snapshot.cpus * 0.8) if running == 0 else 1.0
    allowed = free >= threshold and snapshot.busy < snapshot.cpus * 0.95
    return Decision(allowed, "CPU 여유 활용" if allowed else "CPU 부하/미반영 예약 대기", limit)


def cgroup_paths(proc: Path = Path("/proc")):
    """현재 membership와 mount root를 연결하며 모든 조상 제한을 반환한다."""
    memberships = [line.split(":", 2) for line in (proc / "self/cgroup").read_text().splitlines()]
    paths = []
    for line in (proc / "self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields, mount = left.split(), right.split()
        if mount[0] not in {"cgroup", "cgroup2"}:
            continue
        root, point = Path(fields[3]), Path(fields[4].replace("\\040", " "))
        controllers = set(mount[2].split(","))
        for _, names, location in memberships:
            if mount[0] == "cgroup2" and names or mount[0] == "cgroup" and not controllers.intersection(names.split(",")):
                continue
            try:
                relative = Path(location).relative_to(root)
            except ValueError:
                relative = Path(location.lstrip("/"))
            path = (point / relative).resolve()
            if not path.is_relative_to(point.resolve()):
                raise ValueError("cgroup 경로가 mount 범위를 벗어났습니다.")
            while True:
                paths.append((mount[0], path))
                if path == point.resolve():
                    break
                path = path.parent
    return paths


def linux_sample(proc: Path = Path("/proc")):
    allowed = os.sched_getaffinity(0)
    cpus = float(len(allowed))
    total, busy = 0, 0
    for line in (proc / "stat").read_text().splitlines():
        fields = line.split()
        if fields and fields[0].startswith("cpu") and fields[0][3:].isdecimal() and int(fields[0][3:]) in allowed:
            values = [int(value) for value in fields[1:9]]
            total += sum(values)
            busy += sum(values) - values[3] - values[4]
    memory = {line.split(":")[0]: int(line.split()[1]) * 1024
              for line in (proc / "meminfo").read_text().splitlines() if ":" in line}
    available = memory.get("MemAvailable")
    usage = None
    quota_without_usage = False
    for version, path in cgroup_paths(proc):
        if version == "cgroup2":
            selected_usage = usage is None
            quota = path / "cpu.max"
            if quota.is_file():
                amount, period = quota.read_text().split()
                if amount != "max":
                    capacity = int(amount) / int(period)
                    if capacity <= cpus:
                        cpus, selected_usage = capacity, True
            limit, current = path / "memory.max", path / "memory.current"
            stats = path / "cpu.stat"
            if selected_usage and stats.is_file():
                data = dict(line.split() for line in stats.read_text().splitlines())
                usage = int(data["usage_usec"]) / 1_000_000
                quota_without_usage = False
            elif selected_usage and quota.is_file() and quota.read_text().split()[0] != "max":
                quota_without_usage = True
        else:
            selected_usage = usage is None
            quota, period = path / "cpu.cfs_quota_us", path / "cpu.cfs_period_us"
            if quota.is_file() and period.is_file() and int(quota.read_text()) > 0:
                capacity = int(quota.read_text()) / int(period.read_text())
                if capacity <= cpus:
                    cpus, selected_usage = capacity, True
            limit, current = path / "memory.limit_in_bytes", path / "memory.usage_in_bytes"
            stats = path / "cpuacct.usage"
            if selected_usage and stats.is_file():
                usage = int(stats.read_text()) / 1_000_000_000
            elif selected_usage and quota.is_file() and int(quota.read_text()) > 0:
                quota_without_usage = True
        if limit.is_file() and current.is_file():
            text = limit.read_text().strip()
            if text != "max" and int(text) < 1 << 60:
                free = max(0, int(text) - int(current.read_text()))
                available = min(available, free) if available is not None else free
    if cpus <= 0 or total <= 0:
        raise ValueError("허용 CPU 관측 실패")
    return cpus, total, busy, available, None if quota_without_usage else usage, len(allowed)


def windows_sample():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    process_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
    kernel.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
    if not kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(), ctypes.byref(process_mask), ctypes.byref(system_mask)):
        raise ctypes.WinError(ctypes.get_last_error())
    times = [ctypes.c_ulonglong() for _ in range(3)]
    if not kernel.GetSystemTimes(*(ctypes.byref(value) for value in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    class Memory(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
            (name, ctypes.c_ulonglong) for name in ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
    memory = Memory()
    memory.length = ctypes.sizeof(memory)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise ctypes.WinError(ctypes.get_last_error())
    total = times[1].value + times[2].value
    return float(process_mask.value.bit_count()), total, total - times[0].value, min(memory.available, memory.page_available), None, system_mask.value.bit_count()


class ResourceSampler:
    def __init__(self, *, clock=time.monotonic, read=None):
        self.clock = clock
        self.read = read or (windows_sample if os.name == "nt" else linux_sample)
        self.previous = None
        self.snapshot = None

    def sample(self):
        now = self.clock()
        if self.snapshot is not None and now - self.snapshot.at < 0.2:
            return self.snapshot
        try:
            cpus, total, busy, available, usage, host_cpus = self.read()
            if not math.isfinite(cpus) or cpus <= 0:
                raise ValueError("허용 CPU 관측 실패")
            used = None
            if self.previous is not None:
                then, old_total, old_busy, old_usage = self.previous
                if total > old_total and busy >= old_busy and now > then and not (usage is not None and old_usage is not None and usage < old_usage):
                    host_free = host_cpus * (1 - (busy - old_busy) / (total - old_total))
                    used = min(cpus, host_cpus - host_free) if os.name == "nt" else max(0, cpus - host_free)
                    if usage is not None and old_usage is not None:
                        used = max(used, (usage - old_usage) / (now - then))
                    elif cpus < host_cpus:
                        used = None
            self.previous = now, total, busy, usage
            self.snapshot = Snapshot(now, cpus, used, available,
                                     "Windows system/affinity" if os.name == "nt" else "Linux affinity/proc/cgroup",
                                     "CPU 범위/차이 관측 불가" if used is None else "",
                                     int(cpus) if os.name == "nt" else host_cpus,
                                     cpus if os.name != "nt" and cpus < host_cpus else None)
        except (OSError, ValueError, KeyError, ZeroDivisionError, AttributeError) as error:
            self.previous = None
            previous = self.snapshot
            self.snapshot = Snapshot(now, previous.cpus if previous else float(os.cpu_count() or 1),
                                     None, previous.available if previous else None,
                                     "unavailable: last memory retained" if previous else "unavailable", type(error).__name__)
        return self.snapshot


def process_memory():
    """관측 가능한 현재 프로세스 RSS와 peak를 bytes로 반환한다."""
    if os.name != "nt":
        try:
            values = {line.split(":")[0]: int(line.split()[1]) * 1024
                      for line in Path("/proc/self/status").read_text().splitlines()
                      if line.startswith(("VmRSS:", "VmHWM:"))}
            return {"rss": values["VmRSS"], "peak": values["VmHWM"]}
        except (OSError, KeyError, ValueError):
            return None
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("faults", ctypes.c_uint32)] + [
            (name, ctypes.c_size_t) for name in ("peak", "rss", "paged_peak", "paged", "nonpaged_peak", "nonpaged", "pagefile", "pagefile_peak")]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), ctypes.c_uint32]
    counters = Counters()
    counters.size = ctypes.sizeof(counters)
    if not kernel.K32GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.size):
        return None
    return {"rss": counters.rss, "peak": counters.peak}
