"""
File-based logging for parallel task runs.

Sets up a per-run directory under data/logs/<timestamp>_<githash>/
and provides a thread-local TeeWriter so each task's output goes
to both stdout (with colours) and its own <task_id>.log file
(ANSI-stripped, with timestamp and level prefix).
"""

import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# Matches all ANSI/VT100 escape sequences (colours, cursor moves, etc.)
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Thread-local: .handle = open file, .buf = incomplete line accumulator
_tl: threading.local = threading.local()

_real_stdout = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _detect_level(line: str) -> str:
    lower = line.lower()
    if any(kw in lower for kw in ("error", "failed", "exception", "traceback", "err ")):
        return "ERROR"
    if any(kw in lower for kw in ("rate limit", "warn", "skip", "retry", "interrupt", "timeout")):
        return "WARN"
    return "INFO"


def _write_log_line(fh, raw_line: str) -> None:
    """Strip ANSI codes, prepend timestamp+level, write one line to fh."""
    clean = _ANSI_RE.sub("", raw_line)
    if not clean.strip():
        fh.write("\n")
        return
    level = _detect_level(clean)
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]  # HH:MM:SS.mmm
    fh.write(f"{ts} [{level}] {clean}\n")


def _flush_buf(fh, buf: str) -> None:
    """Write any remaining (newline-less) content in buf as a final line."""
    if buf.strip():
        _write_log_line(fh, buf)


# ---------------------------------------------------------------------------
# TeeWriter
# ---------------------------------------------------------------------------

class _TeeWriter:
    """
    Replaces sys.stdout.  Each write() goes to the real stdout unchanged
    and — for the current thread, if a log file is open — to the file with
    ANSI codes stripped and a timestamp/level prefix added per line.
    """

    def __init__(self, real):
        self.real = real

    def write(self, s: str) -> int:
        self.real.write(s)
        fh = getattr(_tl, "handle", None)
        if fh is not None:
            buf = getattr(_tl, "buf", "")
            buf += s
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                _write_log_line(fh, line)
            _tl.buf = buf
        return len(s)

    def flush(self) -> None:
        self.real.flush()
        fh = getattr(_tl, "handle", None)
        if fh is not None:
            fh.flush()

    def __getattr__(self, name):
        return getattr(self.real, name)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def setup_run_log_dir(*, debug: bool = False) -> Path:
    """
    Create data/logs/<timestamp>_<githash>-{full,debug}-run/ and install the
    TeeWriter on sys.stdout (idempotent — safe to call multiple times).

    The full-run / debug-run suffix mirrors the MLflow experiment naming so
    the two observability stores stay easy to correlate.

    Returns the Path to the new run directory.
    """
    global _real_stdout

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    git_hash = _get_git_hash()
    run_kind = "debug-run" if debug else "full-run"
    run_dir = Path(__file__).parent / "data" / "logs" / f"{timestamp}_{git_hash}-{run_kind}"
    run_dir.mkdir(parents=True, exist_ok=True)

    if not isinstance(sys.stdout, _TeeWriter):
        _real_stdout = sys.stdout
        sys.stdout = _TeeWriter(_real_stdout)

    return run_dir


@contextmanager
def task_log(task_id: str, log_dir: Path):
    """
    Context manager for a worker thread.  Opens <task_id>.log, sets it as
    the thread-local file handle, and flushes/closes it on exit.
    """
    log_path = log_dir / f"{task_id}.log"
    with open(log_path, "w", encoding="utf-8") as fh:
        _tl.handle = fh
        _tl.buf = ""
        try:
            yield log_path
        finally:
            _flush_buf(fh, getattr(_tl, "buf", ""))
            _tl.handle = None
            _tl.buf = ""


@contextmanager
def run_log(log_dir: Path):
    """
    Context manager for the main (orchestrator) thread.  Writes to run.log.
    """
    log_path = log_dir / "run.log"
    with open(log_path, "w", encoding="utf-8") as fh:
        _tl.handle = fh
        _tl.buf = ""
        try:
            yield log_path
        finally:
            _flush_buf(fh, getattr(_tl, "buf", ""))
            _tl.handle = None
            _tl.buf = ""
