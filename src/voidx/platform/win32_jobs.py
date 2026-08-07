from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from typing import Any


INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
DUPLICATE_SAME_ACCESS = 0x00000002
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
STARTF_USESTDHANDLES = 0x00000100
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_NEW_PROCESS_GROUP = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class OwnedJob:
    def __init__(self, handle: int) -> None:
        self.handle = handle
        self._broker_handle: int | None = None
        self._closed = False

    def inheritable_broker_handle(self) -> int:
        if self._broker_handle is None:
            self._broker_handle = _duplicate_inheritable_handle(self.handle)
        return self._broker_handle

    def release_broker_handle(self) -> None:
        if self._broker_handle is None:
            return
        _close_handle(self._broker_handle)
        self._broker_handle = None

    def terminate(self) -> None:
        if self._closed:
            return
        if not kernel32.TerminateJobObject(wintypes.HANDLE(self.handle), 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._closed:
            return
        self.release_broker_handle()
        self._closed = True
        if not kernel32.CloseHandle(wintypes.HANDLE(self.handle)):
            raise ctypes.WinError(ctypes.get_last_error())


def _require_windows() -> None:
    if os.name != "nt" or kernel32 is None:
        raise RuntimeError("Windows Job Objects are only available on Windows")


def _raise_last_error() -> None:
    raise ctypes.WinError(ctypes.get_last_error())


def create_kill_on_close_job() -> OwnedJob:
    _require_windows()
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        _raise_last_error()
    job = OwnedJob(handle)
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        wintypes.HANDLE(handle),
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        job.close()
        _raise_last_error()
    return job


def broker_command(mode: str, command: list[str]) -> list[str]:
    return [sys.executable, "-m", "voidx.platform.win32_jobs", "--broker", mode, *command]


def _duplicate_inheritable_handle(handle: int) -> int:
    current = kernel32.GetCurrentProcess()
    duplicate = wintypes.HANDLE()
    ok = kernel32.DuplicateHandle(
        current,
        wintypes.HANDLE(handle),
        current,
        ctypes.byref(duplicate),
        0,
        True,
        DUPLICATE_SAME_ACCESS,
    )
    if not ok:
        _raise_last_error()
    return int(duplicate.value)


def broker_environment(job: OwnedJob, env: dict[str, str] | None) -> dict[str, str]:
    broker_env = dict(os.environ if env is None else env)
    broker_env["VOIDX_WIN32_JOB_HANDLE"] = str(job.inheritable_broker_handle())
    return broker_env


def startupinfo_for_job(job: OwnedJob) -> subprocess.STARTUPINFO:
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.lpAttributeList = {"handle_list": [job.inheritable_broker_handle()]}
    return startupinfo


def _handle_from_env() -> int:
    raw = os.environ.get("VOIDX_WIN32_JOB_HANDLE")
    if not raw:
        raise RuntimeError("VOIDX_WIN32_JOB_HANDLE is not set")
    return int(raw)


def _assign_current_process_to_job(job_handle: int) -> None:
    _require_windows()
    current = kernel32.GetCurrentProcess()
    if not kernel32.AssignProcessToJobObject(wintypes.HANDLE(job_handle), current):
        _raise_last_error()


def _close_handle(handle: int) -> None:
    if handle and handle != INVALID_HANDLE_VALUE:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _run_broker(mode: str, command: list[str]) -> int:
    job_handle = _handle_from_env()
    _assign_current_process_to_job(job_handle)
    _close_handle(job_handle)
    if mode == "exec":
        process = subprocess.Popen(command)
    elif mode == "shell":
        process = subprocess.Popen(command[0], shell=True)
    else:
        raise RuntimeError(f"Unknown broker mode: {mode}")
    return process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", action="store_true")
    parser.add_argument("mode", choices=["exec", "shell"])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not args.broker:
        parser.error("only broker mode is supported")
    if not args.command:
        parser.error("command is required")
    return _run_broker(args.mode, args.command)


if __name__ == "__main__":
    raise SystemExit(main())
