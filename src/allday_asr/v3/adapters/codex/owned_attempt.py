"""Only handles to processes this harness creates; never PID-tree termination."""

import ctypes
import os
import subprocess
import time
from ctypes import wintypes as W

K = ctypes.WinDLL("kernel32", use_last_error=True)
N = ctypes.WinDLL("ntdll")
HANDLE = W.HANDLE
SIZE = ctypes.c_size_t


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", W.DWORD),
        ("MinimumWorkingSetSize", SIZE),
        ("MaximumWorkingSetSize", SIZE),
        ("ActiveProcessLimit", W.DWORD),
        ("Affinity", SIZE),
        ("PriorityClass", W.DWORD),
        ("SchedulingClass", W.DWORD),
    ]


class IO(ctypes.Structure):
    _fields_ = [
        (n, ctypes.c_ulonglong)
        for n in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class Accounting(ctypes.Structure):
    _fields_ = [(n, ctypes.c_longlong) for n in (
        "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")]
    _fields_ += [(n, W.DWORD) for n in (
        "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]


class ProcessIds(ctypes.Structure):
    _fields_ = [("assigned", W.DWORD), ("count", W.DWORD), ("ids", SIZE * 8192)]


class Limits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", BasicLimits),
        ("IoInfo", IO),
        ("ProcessMemoryLimit", SIZE),
        ("JobMemoryLimit", SIZE),
        ("PeakProcessMemoryUsed", SIZE),
        ("PeakJobMemoryUsed", SIZE),
    ]


K.CreateJobObjectW.argtypes = [ctypes.c_void_p, W.LPCWSTR]
K.CreateJobObjectW.restype = HANDLE
K.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
K.SetInformationJobObject.restype = W.BOOL
K.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
K.AssignProcessToJobObject.restype = W.BOOL
K.TerminateJobObject.argtypes = [HANDLE, W.UINT]
K.TerminateJobObject.restype = W.BOOL
K.QueryInformationJobObject.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.c_void_p]
K.QueryInformationJobObject.restype = W.BOOL
K.CloseHandle.argtypes = [HANDLE]
K.CloseHandle.restype = W.BOOL
K.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
K.OpenProcess.restype = HANDLE
K.WaitForSingleObject.argtypes = [HANDLE, W.DWORD]
K.WaitForSingleObject.restype = W.DWORD
K.GetProcessTimes.argtypes = [HANDLE, *([ctypes.POINTER(W.FILETIME)] * 4)]
K.GetProcessTimes.restype = W.BOOL
N.NtResumeProcess.argtypes = [HANDLE]
N.NtResumeProcess.restype = ctypes.c_long


def checked(ok):
    if not ok:
        raise OSError(ctypes.get_last_error(), "SAFE_TIMEOUT_CONTROL_UNAVAILABLE")


def created(handle):
    values = [W.FILETIME() for _ in range(4)]
    checked(K.GetProcessTimes(handle, *map(ctypes.byref, values)))
    return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime


class OwnedAttempt:
    def __init__(self, argv, log, cwd):
        self.job = K.CreateJobObjectW(None, None)
        checked(self.job)
        limits = Limits()
        limits.BasicLimitInformation.LimitFlags = (
            0x2000  # KILL_ON_JOB_CLOSE; no breakaway.
        )
        checked(
            K.SetInformationJobObject(
                self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            )
        )
        self.output = open(log, "wb")
        self.proc = None
        self.closed = False
        self.terminated = False
        try:
            self.proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdout=self.output,
                stderr=subprocess.STDOUT,
                creationflags=0x4 | 0x08000000,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.creation = created(HANDLE(int(self.proc._handle)))
            self.identity = {
                "pid": self.proc.pid,
                "parent_pid": os.getpid(),
                "command_line": subprocess.list2cmdline(argv),
                "process_creation_filetime": self.creation,
                "launched_suspended": True,
                "assigned_before_any_child_or_model_call": False,
                "exclusive_job_handle": int(self.job),
            }
            checked(
                K.AssignProcessToJobObject(self.job, HANDLE(int(self.proc._handle)))
            )
            self.identity["assigned_before_any_child_or_model_call"] = True
            if N.NtResumeProcess(HANDLE(int(self.proc._handle))) < 0:
                raise RuntimeError("SAFE_TIMEOUT_CONTROL_UNAVAILABLE: resume failed")
        except BaseException:
            # Own suspended process handle only; no descendants could have started.
            if self.proc is not None and self.proc.poll() is None:
                self.proc.kill()
            self.close()
            raise

    def stop(self):
        if self.closed:
            return
        if self.proc is not None:
            if created(HANDLE(int(self.proc._handle))) != self.creation:
                raise RuntimeError(
                    "SAFE_TIMEOUT_CONTROL_UNAVAILABLE: handle creation changed"
                )
        self._terminate_tree()
        self.identity["termination"] = "EXCLUSIVE_ASSIGNED_JOB_HANDLE_ONLY"

    def _terminate_tree(self):
        if self.terminated:
            return
        members = ProcessIds()
        checked(K.QueryInformationJobObject(self.job, 3, ctypes.byref(members), ctypes.sizeof(members), None))
        # Acquire wait-only handles while membership is known. Termination always
        # targets the exclusive Job handle, never a looked-up PID.
        handles = [K.OpenProcess(0x100000, False, pid) for pid in members.ids[:members.count]]
        try:
            checked(K.TerminateJobObject(self.job, 124))
            deadline = time.monotonic() + 3
            for handle in handles:
                if handle:
                    remaining = max(0, int((deadline - time.monotonic()) * 1000))
                    if K.WaitForSingleObject(handle, remaining) != 0:
                        raise RuntimeError("SAFE_TIMEOUT_CONTROL_UNAVAILABLE: owned process did not stop")
            self.terminated = True
        finally:
            for handle in handles:
                if handle:
                    checked(K.CloseHandle(handle))

    def wait(self, timeout, cancel_event=None):
        deadline = time.monotonic() + timeout
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self.stop()
                    self.proc.wait(timeout=0.5)
                    return 125
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.stop()
                    self.proc.wait(timeout=0.5)
                    return 124
                try:
                    return self.proc.wait(timeout=min(0.25, remaining))
                except subprocess.TimeoutExpired:
                    pass
        finally:
            self.close()

    def close(self):
        if not self.closed:
            if self.job:
                # A returned parent may still own descendants holding audio/GPU/log handles.
                # Wait for the exclusive job, not just Popen's parent process.
                self._terminate_tree()
                deadline = time.monotonic() + 3
                while True:
                    accounting = Accounting()
                    checked(K.QueryInformationJobObject(self.job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None))
                    if not accounting.ActiveProcesses:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("SAFE_TIMEOUT_CONTROL_UNAVAILABLE: owned job did not stop")
                    time.sleep(0.01)
                checked(K.CloseHandle(self.job))
            self.closed = True
            self.output.close()
            if self.proc is not None:
                self.proc.wait(timeout=0.5)
                self.proc._handle.Close()
