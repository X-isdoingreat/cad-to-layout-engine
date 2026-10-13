from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence


DEFAULT_MEMORY_LIMIT_BYTES = 2 * 1024**3
DEFAULT_TIMEOUT_SECONDS = 600.0
TERMINAL_INSPECTION_NAME = "complex_dwg_inspection.json"
FAILURE_RECEIPT_NAME = "complex_dwg_failure.json"


@dataclass(frozen=True)
class BoundedInspectionResult:
    """Outcome returned to the CLI without confusing structure inspection with extraction."""

    status: str
    run_id: str
    inspection_path: str | None
    failure_receipt_path: str | None
    elapsed_seconds: float
    peak_private_memory_bytes: int
    memory_limit_bytes: int
    timeout_seconds: float
    return_code: int | None
    failure_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_summary(source: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {"path": str(source)}
    try:
        stat_before = source.stat()
        summary["size_bytes"] = stat_before.st_size
        with source.open("rb") as stream:
            summary["format_version"] = stream.read(6).decode("ascii", "replace")
        summary["sha256"] = _sha256(source)
        stat_after = source.stat()
        summary["modified_time_ns"] = stat_after.st_mtime_ns
        if (stat_before.st_size, stat_before.st_mtime_ns) != (
            stat_after.st_size,
            stat_after.st_mtime_ns,
        ):
            summary["identity_error"] = "input changed while its fallback identity was recorded"
    except OSError as exc:
        summary["identity_error"] = f"{type(exc).__name__}: {exc}"
    return summary


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _tail(path: Path, limit: int = 16 * 1024) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            return stream.read().decode("utf-8", "replace")
    except OSError:
        return ""


if os.name == "nt":
    from ctypes import wintypes

    ULONG_PTR = ctypes.c_size_t
    SIZE_T = ctypes.c_size_t

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ULONG_PTR),
            ("MaximumWorkingSetSize", ULONG_PTR),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ULONG_PTR),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", SIZE_T),
            ("JobMemoryLimit", SIZE_T),
            ("PeakProcessMemoryUsed", SIZE_T),
            ("PeakJobMemoryUsed", SIZE_T),
        ]

    class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", SIZE_T),
            ("WorkingSetSize", SIZE_T),
            ("QuotaPeakPagedPoolUsage", SIZE_T),
            ("QuotaPagedPoolUsage", SIZE_T),
            ("QuotaPeakNonPagedPoolUsage", SIZE_T),
            ("QuotaNonPagedPoolUsage", SIZE_T),
            ("PagefileUsage", SIZE_T),
            ("PeakPagefileUsage", SIZE_T),
            ("PrivateUsage", SIZE_T),
        ]


class _WindowsJob:
    """A Job Object that bounds aggregate committed memory for the process tree."""

    _JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, memory_limit_bytes: int):
        if os.name != "nt":
            raise RuntimeError("Windows Job Objects are unavailable on this platform")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        self._handle = self._kernel32.CreateJobObjectW(None, None)
        self.peak_query_failed = False
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

        information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = (
            self._JOB_OBJECT_LIMIT_JOB_MEMORY | self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        information.JobMemoryLimit = memory_limit_bytes
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        process_handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def peak_bytes(self) -> int:
        information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        returned = wintypes.DWORD()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
            ctypes.byref(returned),
        ):
            self.peak_query_failed = True
            return 0
        return int(max(information.PeakProcessMemoryUsed, information.PeakJobMemoryUsed))

    def terminate(self) -> None:
        if self._handle:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if getattr(self, "_handle", None):
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class _PosixMemoryController:
    def assign(self, process: subprocess.Popen[bytes]) -> None:
        del process

    def peak_bytes(self) -> int:
        return 0

    def terminate(self) -> None:
        return None

    def close(self) -> None:
        return None


class _InheritedWholeChainJob:
    """No-op child controller used only after verifying outer Job inheritance.

    The whole-chain supervisor owns the hard aggregate limit. Descendant
    converter launchers must remain in that Job instead of attempting to form a
    second, independently measured process-only Job.
    """

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        del process

    def peak_bytes(self) -> int:
        return 0

    def terminate(self) -> None:
        return None

    def close(self) -> None:
        return None


def _current_process_is_in_job() -> bool:
    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    result = wintypes.BOOL()
    if not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(result.value)


def _make_memory_controller(
    memory_limit_bytes: int,
) -> _WindowsJob | _PosixMemoryController | _InheritedWholeChainJob:
    if os.name == "nt":
        if os.environ.get("CAD2LAYOUT_INHERITED_WHOLE_CHAIN_JOB") == "1":
            if not _current_process_is_in_job():
                raise RuntimeError(
                    "whole-chain Job inheritance was requested but the current process is not in a Job"
                )
            return _InheritedWholeChainJob()
        return _WindowsJob(memory_limit_bytes)
    return _PosixMemoryController()


def _resume_windows_suspended_process(process: subprocess.Popen[bytes]) -> None:
    """Resume the initial thread of a CREATE_SUSPENDED subprocess.

    ``subprocess.Popen`` closes the thread handle returned by ``CreateProcess``.
    The documented Toolhelp APIs are therefore used to locate the process'
    still-suspended initial thread. Failure is fatal so no worker can run
    outside the Job assignment boundary.
    """

    if os.name != "nt":
        return

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_THREADENTRY32),
    ]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_THREADENTRY32),
    ]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or int(snapshot) == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    thread_ids: list[int] = []
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            while True:
                if int(entry.th32OwnerProcessID) == process.pid:
                    thread_ids.append(int(entry.th32ThreadID))
                entry.dwSize = ctypes.sizeof(entry)
                if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)

    if not thread_ids:
        raise RuntimeError("cannot locate the suspended worker's initial thread")
    thread = kernel32.OpenThread(0x0002, False, thread_ids[0])  # THREAD_SUSPEND_RESUME
    if not thread:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        previous_suspend_count = kernel32.ResumeThread(thread)
        if previous_suspend_count == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        if previous_suspend_count < 1:
            raise RuntimeError("worker thread was not suspended before Job assignment")
    finally:
        kernel32.CloseHandle(thread)


def _posix_limit(memory_limit_bytes: int):
    if os.name == "nt":
        return None

    def apply_limit() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))

    return apply_limit


def _process_private_bytes(process: subprocess.Popen[bytes]) -> int:
    if process.poll() is not None:
        return 0
    if os.name == "nt":
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = _PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        process_handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
        if psapi.GetProcessMemoryInfo(process_handle, ctypes.byref(counters), counters.cb):
            return int(counters.PrivateUsage)
        return 0
    status = Path(f"/proc/{process.pid}/status")
    try:
        for line in status.read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _build_worker_command(
    python_executable: str,
    source: Path,
    inspection_path: Path,
    receipt_path: Path,
    start_gate: Path,
) -> list[str]:
    return [
        python_executable,
        "-X",
        "utf8",
        "-m",
        "cad_to_layout.dwg_worker",
        "--source",
        str(source),
        "--inspection",
        str(inspection_path),
        "--receipt",
        str(receipt_path),
        "--start-gate",
        str(start_gate),
    ]


def _validate_worker_success(
    inspection: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
) -> str | None:
    if receipt is None or receipt.get("status") != "complete":
        return "worker did not publish a complete receipt"
    if inspection is None or inspection.get("status") != "complete":
        return "worker did not publish a complete inspection"
    source = inspection.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("sha256"), str) or len(source["sha256"]) != 64:
        return "inspection is not bound to a SHA-256 source identity"
    structure = inspection.get("structure")
    required_counts = ("layer_count", "block_count", "modelspace_entity_count")
    if not isinstance(structure, dict) or any(not isinstance(structure.get(key), int) for key in required_counts):
        return "inspection structure counts are incomplete"
    stages = receipt.get("stage_timings")
    required_stages = ("source_identity", "aspose_load", "basic_tables", "modelspace_scan", "dispose")
    if not isinstance(stages, dict) or any(stage not in stages for stage in required_stages):
        return "worker stage timings are incomplete"
    if any(not isinstance(stages[stage], dict) or stages[stage].get("status") != "complete" for stage in required_stages):
        return "one or more worker stages did not complete"
    return None


def _classify_failure(
    *,
    timed_out: bool,
    return_code: int | None,
    peak_bytes: int,
    memory_limit_bytes: int,
    worker_receipt: dict[str, Any] | None,
    supervisor_error: str | None,
    stderr_tail: str = "",
) -> str:
    if timed_out:
        return "timeout"
    if supervisor_error:
        return "supervisor_error"
    if worker_receipt and isinstance(worker_receipt.get("failure_kind"), str):
        return str(worker_receipt["failure_kind"])
    if "out of memory" in stderr_tail.casefold():
        return "backend_out_of_memory"
    unsigned_code = None if return_code is None else return_code & 0xFFFFFFFF
    if unsigned_code in {0xC0000017, 0xC000012D}:
        return "memory_limit"
    if return_code not in {None, 0} and peak_bytes >= int(memory_limit_bytes * 0.95):
        return "memory_limit"
    if return_code not in {None, 0}:
        return "worker_exception"
    return "invalid_worker_output"


def _terminate_worker(
    process: subprocess.Popen[bytes], controller: _WindowsJob | _PosixMemoryController
) -> None:
    controller.terminate()
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass


def run_bounded_inspection(
    source: str | Path,
    output_dir: str | Path,
    *,
    memory_limit_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    python_executable: str = sys.executable,
    environment: dict[str, str] | None = None,
) -> BoundedInspectionResult:
    """Run structure-only DWG inspection with an atomic success/failure boundary.

    A controlled failure never replaces ``complex_dwg_inspection.json``. The
    returned result points at either that terminal inspection or a failure
    receipt, allowing a future CLI to select its exit code without parsing logs.
    """

    if memory_limit_bytes <= 0:
        raise ValueError("memory_limit_bytes must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    source_path = Path(source).resolve()
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    final_inspection = output_path / TERMINAL_INSPECTION_NAME
    failure_receipt_path = output_path / FAILURE_RECEIPT_NAME
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    previous_inspection = {
        "path": str(final_inspection),
        "exists": final_inspection.is_file(),
        "sha256": _sha256(final_inspection) if final_inspection.is_file() else None,
    }

    def fail_without_worker(failure_kind: str, message: str) -> BoundedInspectionResult:
        elapsed = time.monotonic() - started
        receipt = {
            "schema_version": "1.0",
            "status": "controlled_failure",
            "run_id": run_id,
            "failure_kind": failure_kind,
            "message": message,
            "source": _source_summary(source_path),
            "resources": {
                "elapsed_seconds": round(elapsed, 6),
                "timeout_seconds": timeout_seconds,
                "peak_private_memory_bytes": 0,
                "memory_limit_bytes": memory_limit_bytes,
            },
            "terminal_inspection": {
                "updated": False,
                "previous_preserved": previous_inspection,
            },
            "fallback_attempted": False,
        }
        _atomic_write_json(failure_receipt_path, receipt)
        return BoundedInspectionResult(
            status="controlled_failure",
            run_id=run_id,
            inspection_path=None,
            failure_receipt_path=str(failure_receipt_path),
            elapsed_seconds=elapsed,
            peak_private_memory_bytes=0,
            memory_limit_bytes=memory_limit_bytes,
            timeout_seconds=timeout_seconds,
            return_code=None,
            failure_kind=failure_kind,
        )

    if not source_path.is_file():
        return fail_without_worker("source_invalid", "input DWG does not exist or is not a file")

    staging_root = Path(tempfile.mkdtemp(prefix=".bounded-inspect-", dir=output_path))
    staging_inspection = staging_root / "inspection.json"
    staging_receipt = staging_root / "worker_receipt.json"
    start_gate = staging_root / "start.gate"
    stdout_path = staging_root / "worker.stdout.log"
    stderr_path = staging_root / "worker.stderr.log"
    process: subprocess.Popen[bytes] | None = None
    controller: _WindowsJob | _PosixMemoryController | None = None
    return_code: int | None = None
    timed_out = False
    supervisor_error: str | None = None
    observed_peak = 0

    worker_environment = os.environ.copy()
    if environment:
        worker_environment.update(environment)
    worker_environment.update(
        {
            "CAD_INSPECT_SOURCE": str(source_path),
            "CAD_INSPECT_STAGING_INSPECTION": str(staging_inspection),
            "CAD_INSPECT_STAGING_RECEIPT": str(staging_receipt),
            "CAD_INSPECT_START_GATE": str(start_gate),
            "CAD_INSPECT_MEMORY_LIMIT_BYTES": str(memory_limit_bytes),
            "CAD_INSPECT_TIMEOUT_SECONDS": str(timeout_seconds),
            "CAD_INSPECT_RUN_ID": run_id,
        }
    )

    try:
        controller = _make_memory_controller(memory_limit_bytes)
        command: Sequence[str] = _build_worker_command(
            python_executable,
            source_path,
            staging_inspection,
            staging_receipt,
            start_gate,
        )
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
            process = subprocess.Popen(
                command,
                cwd=str(Path(__file__).resolve().parents[1]),
                env=worker_environment,
                stdout=stdout_stream,
                stderr=stderr_stream,
                creationflags=creationflags,
                preexec_fn=_posix_limit(memory_limit_bytes),
                start_new_session=os.name != "nt",
            )
            try:
                controller.assign(process)
            except Exception as exc:
                supervisor_error = f"cannot apply hard process-memory limit: {type(exc).__name__}: {exc}"
                _terminate_worker(process, controller)
            else:
                start_gate.write_bytes(b"start\n")
                deadline = time.monotonic() + timeout_seconds
                while process.poll() is None:
                    observed_peak = max(
                        observed_peak,
                        controller.peak_bytes(),
                        _process_private_bytes(process),
                    )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        _terminate_worker(process, controller)
                        break
                    time.sleep(min(0.05, remaining))
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_worker(process, controller)
                return_code = process.wait(timeout=5)
            observed_peak = max(observed_peak, controller.peak_bytes())
    except Exception as exc:
        supervisor_error = f"{type(exc).__name__}: {exc}"
        if process is not None and process.poll() is None and controller is not None:
            _terminate_worker(process, controller)
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return_code = None
    finally:
        if controller is not None:
            observed_peak = max(observed_peak, controller.peak_bytes())
            controller.close()

    elapsed = time.monotonic() - started
    worker_receipt = _read_json(staging_receipt)
    worker_inspection = _read_json(staging_inspection)
    stdout_tail = _tail(stdout_path)
    stderr_tail = _tail(stderr_path)
    validation_error = _validate_worker_success(worker_inspection, worker_receipt)
    succeeded = (
        not timed_out
        and supervisor_error is None
        and return_code == 0
        and validation_error is None
    )

    if succeeded:
        assert worker_inspection is not None
        assert worker_receipt is not None
        worker_inspection["run_id"] = run_id
        worker_inspection["stage_timings"] = worker_receipt["stage_timings"]
        worker_inspection["resources"] = {
            "elapsed_seconds": round(elapsed, 6),
            "timeout_seconds": timeout_seconds,
            "peak_private_memory_bytes": observed_peak,
            "memory_limit_bytes": memory_limit_bytes,
            "memory_limit_enforcement": "windows_job_object" if os.name == "nt" else "posix_rlimit_as",
        }
        worker_inspection["capability_boundary"] = "structure_inspection_only"
        _atomic_write_json(final_inspection, worker_inspection)
        failure_receipt_path.unlink(missing_ok=True)
        shutil.rmtree(staging_root, ignore_errors=True)
        return BoundedInspectionResult(
            status="complete",
            run_id=run_id,
            inspection_path=str(final_inspection),
            failure_receipt_path=None,
            elapsed_seconds=elapsed,
            peak_private_memory_bytes=observed_peak,
            memory_limit_bytes=memory_limit_bytes,
            timeout_seconds=timeout_seconds,
            return_code=return_code,
        )

    failure_kind = _classify_failure(
        timed_out=timed_out,
        return_code=return_code,
        peak_bytes=observed_peak,
        memory_limit_bytes=memory_limit_bytes,
        worker_receipt=worker_receipt,
        supervisor_error=supervisor_error,
        stderr_tail=stderr_tail,
    )
    message = supervisor_error or validation_error or "bounded DWG worker did not complete"
    if failure_kind == "backend_out_of_memory":
        message = "CAD backend terminated with an out-of-memory error before publishing a complete receipt"
    receipt = {
        "schema_version": "1.0",
        "status": "controlled_failure",
        "run_id": run_id,
        "failure_kind": failure_kind,
        "message": message,
        "source": (worker_receipt or {}).get("source", _source_summary(source_path)),
        "resources": {
            "elapsed_seconds": round(elapsed, 6),
            "timeout_seconds": timeout_seconds,
            "peak_private_memory_bytes": observed_peak,
            "memory_limit_bytes": memory_limit_bytes,
            "memory_limit_enforcement": "windows_job_object" if os.name == "nt" else "posix_rlimit_as",
        },
        "process": {
            "return_code": return_code,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        },
        "stage_timings": (worker_receipt or {}).get("stage_timings", {}),
        "worker_failure": (worker_receipt or {}).get("failure", None),
        "terminal_inspection": {
            "updated": False,
            "previous_preserved": previous_inspection,
        },
        "fallback_attempted": False,
    }
    _atomic_write_json(failure_receipt_path, receipt)
    shutil.rmtree(staging_root, ignore_errors=True)
    return BoundedInspectionResult(
        status="controlled_failure",
        run_id=run_id,
        inspection_path=None,
        failure_receipt_path=str(failure_receipt_path),
        elapsed_seconds=elapsed,
        peak_private_memory_bytes=observed_peak,
        memory_limit_bytes=memory_limit_bytes,
        timeout_seconds=timeout_seconds,
        return_code=return_code,
        failure_kind=failure_kind,
    )
