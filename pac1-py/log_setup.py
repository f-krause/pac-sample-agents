"""
File-based logging for parallel task runs.

Sets up a per-run directory under docs/logs/<timestamp>_<githash>/
and provides a thread-local TeeWriter so each task's output goes
to both stdout and its own <task_id>.log file.
"""

import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# Thread-local storage: each thread holds its own open log file handle.
_task_file: threading.local = threading.local()
_real_stdout = None


def _get_git_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent,
        )
        return result.stdout.strip() or "nogit"
    except Exception:
        return "nogit"


class _TeeWriter:
    """Writes to real stdout AND to the current thread's log file (if set)."""

    def __init__(self, real):
        self.real = real

    def write(self, s: str) -> int:
        self.real.write(s)
        f = getattr(_task_file, "handle", None)
        if f is not None:
            f.write(s)
        return len(s)

    def flush(self) -> None:
        self.real.flush()
        f = getattr(_task_file, "handle", None)
        if f is not None:
            f.flush()

    def __getattr__(self, name):
        return getattr(self.real, name)


def setup_run_log_dir() -> Path:
    """
    Create docs/logs/<timestamp>_<githash>/ and install the TeeWriter on
    sys.stdout (idempotent — safe to call multiple times).

    Returns the Path to the new run directory.
    """
    global _real_stdout

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    git_hash = _get_git_hash()
    run_dir = Path(__file__).parent / "docs" / "logs" / f"{timestamp}_{git_hash}"
    run_dir.mkdir(parents=True, exist_ok=True)

    if not isinstance(sys.stdout, _TeeWriter):
        _real_stdout = sys.stdout
        sys.stdout = _TeeWriter(_real_stdout)

    return run_dir


@contextmanager
def task_log(task_id: str, log_dir: Path):
    """
    Context manager that opens docs/logs/<run>/<task_id>.log for the calling
    thread and closes it when the block exits.  All print() calls from this
    thread inside the block are mirrored to that file.
    """
    log_path = log_dir / f"{task_id}.log"
    with open(log_path, "w", encoding="utf-8") as fh:
        _task_file.handle = fh
        try:
            yield log_path
        finally:
            _task_file.handle = None


@contextmanager
def run_log(log_dir: Path):
    """
    Context manager for the main (orchestrator) thread.  Writes to run.log
    inside the run directory.
    """
    log_path = log_dir / "run.log"
    with open(log_path, "w", encoding="utf-8") as fh:
        _task_file.handle = fh
        try:
            yield log_path
        finally:
            _task_file.handle = None
