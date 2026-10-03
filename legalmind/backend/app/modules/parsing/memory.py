"""解析过程的内存防护（16 GB 单机硬门槛，设计 §14.2）。

解析在独立 worker 进程里执行、任务结束即释放（设计 §3.1）。本模块在解析循环里采样工作集
增长，超过预算就主动中止并给出可诊断错误，避免把整机拖入交换或被系统 OOM 杀掉。
采样失败一律返回 0，不因为拿不到内存数字而中断解析。

注意这是**粗粒度的兜底**：工作集按内存页变动，小文件解析不会让它增长，因此它挡的是
「文件很大、页很多」这类真实 OOM 场景；精细的资源边界由 ``ParseLimits`` 的字节/页数/字符
上限负责。
"""

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Self

from app.modules.parsing.interface import MemoryBudgetExceeded


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _windows_counters() -> _ProcessMemoryCounters | None:
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    # 句柄是 64 位指针，必须声明 argtypes，否则 ctypes 会按 32 位 int 截断而调用失败
    current_process = ctypes.windll.kernel32.GetCurrentProcess  # type: ignore[attr-defined]
    current_process.restype = wintypes.HANDLE
    handle = current_process()
    for dll, name in (("psapi", "GetProcessMemoryInfo"), ("kernel32", "K32GetProcessMemoryInfo")):
        try:
            func = getattr(getattr(ctypes.windll, dll), name)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            continue
        func.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD]
        func.restype = wintypes.BOOL
        if func(handle, ctypes.byref(counters), counters.cb):
            return counters
    return None


def current_rss_bytes() -> int:
    """当前进程常驻内存字节数；无法获取时返回 0。"""
    if sys.platform == "win32":
        counters = _windows_counters()
        return int(counters.WorkingSetSize) if counters else 0
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def peak_working_set_bytes() -> int:
    """进程生命周期内的峰值工作集；无法获取时返回 0。

    单调不减，适合给短任务测量内存：取解析前后的差值即可得到该次解析的峰值下界
    （基准脚本用它补足采样线程在极短任务上的抖动）。
    """
    if sys.platform == "win32":
        counters = _windows_counters()
        return int(counters.PeakWorkingSetSize) if counters else 0
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux 以 KB 计，macOS 以字节计
        return usage * 1024 if sys.platform.startswith("linux") else usage
    except (ImportError, ValueError):
        return 0


class MemoryGuard:
    """采样解析期间的内存增长，超过预算抛 ``MemoryBudgetExceeded``。

    ``budget_bytes <= 0`` 表示不限制（配置关闭）。
    """

    def __init__(self, budget_bytes: int) -> None:
        self.budget_bytes = budget_bytes
        self._baseline = 0
        self._peak = 0

    def __enter__(self) -> Self:
        self._baseline = current_rss_bytes()
        self._peak = self._baseline
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    @property
    def peak_growth_bytes(self) -> int:
        return max(0, self._peak - self._baseline)

    def check(self) -> None:
        if self.budget_bytes <= 0:
            return
        current = current_rss_bytes()
        self._peak = max(self._peak, current)
        if current - self._baseline > self.budget_bytes:
            limit_mb = self.budget_bytes // (1024 * 1024)
            raise MemoryBudgetExceeded(f"Parsing exceeded memory budget of {limit_mb} MB")
