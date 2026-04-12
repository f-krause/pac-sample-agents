"""
collect_tasks.py – snapshot benchmark task environments into data/tasks/.

Usage:
    python collect_tasks.py [task_id ...]   # optional filter by task id
    python collect_tasks.py --runs N        # repeat N times to capture entropy variants

Each run creates a scored run via the BitGN API, starts each trial, captures
the initial environment (instruction, context, file tree, root listing, key
file contents), then ends the trial and submits the run.  Snapshots are saved
as JSON files under:

    data/tasks/<task_id>/<sha256_of_content>.json

Deduplication is content-addressed: identical environments are stored once.
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    GetBenchmarkRequest,
    StartRunRequest,
    StartTrialRequest,
    SubmitRunRequest,
    StatusRequest,
)
from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import ContextRequest, ListRequest, ReadRequest, TreeRequest
from connectrpc.errors import ConnectError
from google.protobuf.json_format import MessageToDict

load_dotenv()

BITGN_URL = os.getenv("BITGN_HOST") or "https://api.bitgn.com"
BITGN_API_KEY = os.getenv("BITGN_API_KEY") or ""
BENCHMARK_ID = os.getenv("BENCH_ID") or "bitgn/pac1-prod"
DATA_DIR = Path(__file__).parent.parent / "data" / "tasks"

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_YELLOW = "\x1B[33m"
CLI_CLR = "\x1B[0m"


def _tree_to_dict(entry) -> dict:
    return {
        "name": entry.name,
        "is_dir": entry.is_dir if hasattr(entry, "is_dir") else bool(entry.children),
        "children": [_tree_to_dict(c) for c in entry.children],
    }


def _collect_flat_paths(entry, prefix: str = "") -> list[str]:
    """Walk a tree entry and return all file paths."""
    path = f"{prefix}/{entry.name}".lstrip("/")
    if not entry.children:
        return [path] if path else []
    paths = []
    for child in entry.children:
        paths.extend(_collect_flat_paths(child, f"/{path}".rstrip("/")))
    return paths


def _read_file(vm: PcmRuntimeClientSync, path: str) -> str | None:
    try:
        return vm.read(ReadRequest(path=path)).content
    except ConnectError:
        return None


def capture_environment(vm: PcmRuntimeClientSync) -> dict:
    """Call read-only RPCs to snapshot the task environment."""
    snapshot: dict = {}

    # Context (simulated time, persona, etc.)
    try:
        ctx = vm.context(ContextRequest())
        snapshot["context"] = MessageToDict(ctx)
    except ConnectError as exc:
        snapshot["context"] = {"error": str(exc.message)}

    # Full file tree (depth 0 = unlimited)
    all_paths: list[str] = []
    try:
        tree = vm.tree(TreeRequest(root="", level=0))
        snapshot["tree"] = _tree_to_dict(tree.root)
        all_paths = _collect_flat_paths(tree.root)
    except ConnectError as exc:
        snapshot["tree"] = {"error": str(exc.message)}

    # Root directory listing
    try:
        listing = vm.list(ListRequest(name="/"))
        snapshot["root_listing"] = [
            {"name": e.name, "is_dir": e.is_dir} for e in listing.entries
        ]
    except ConnectError as exc:
        snapshot["root_listing"] = {"error": str(exc.message)}

    # All AGENTS.md / AGENTS.MD files (root + nested) – primary instruction source
    agents_paths = [
        p for p in all_paths
        if p.split("/")[-1].upper() == "AGENTS.MD"
    ]
    # Also check root variants in case tree missed them
    for fallback in ("AGENTS.md", "AGENTS.MD"):
        if fallback not in agents_paths and f"/{fallback}" not in agents_paths:
            agents_paths.insert(0, fallback)

    # Additional always-relevant instruction files referenced by convention
    extra_instruction_paths = [
        p for p in all_paths
        if any(
            p.endswith(name)
            for name in (
                "soul.md", "Soul.md",                     # "always read soul.md"
                "agent_preferences.md",
                "agent_initiatives.md",
                "agent_changelog.md",
            )
        )
    ]

    # 99_process/ files – "source of truth for repo processes" (sandbox)
    process_paths = [p for p in all_paths if "99_process" in p and p.endswith(".md")]

    # 99_system/ workflow and schema docs (production)
    system_paths = [
        p for p in all_paths
        if ("99_system/workflows/" in p or "99_system/schemas/" in p)
        and p.endswith(".md")
        and p.split("/")[-1].upper() != "AGENTS.MD"  # already captured above
    ]

    instruction_files: dict[str, str] = {}
    for path in dict.fromkeys(agents_paths + extra_instruction_paths + process_paths + system_paths):
        content = _read_file(vm, path)
        if content is not None:
            instruction_files[path] = content

    # Other well-known top-level files
    for path in ("README.md", "CLAUDE.md"):
        if path not in instruction_files:
            content = _read_file(vm, path)
            if content is not None:
                instruction_files[path] = content

    if instruction_files:
        snapshot["instruction_files"] = instruction_files

    return snapshot


def save_snapshot(task_id: str, instruction: str, env: dict, benchmark_description: str = "") -> tuple[bool, Path]:
    """
    Save snapshot to data/tasks/<task_id>/<hash>.json.
    Returns (is_new, path).
    """
    payload = {
        "task_id": task_id,
        "benchmark_description": benchmark_description,
        "instruction": instruction,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "environment": env,
    }
    content = json.dumps(payload, indent=2, ensure_ascii=False)
    # Hash only the stable parts (not timestamp) for dedup
    dedup_payload = {
        "task_id": task_id,
        "instruction": instruction,
        "environment": env,
    }
    content_hash = hashlib.sha256(
        json.dumps(dedup_payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]

    task_dir = DATA_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    out_path = task_dir / f"{content_hash}.json"
    is_new = not out_path.exists()
    if is_new:
        out_path.write_text(content, encoding="utf-8")

    return is_new, out_path


def collect(task_filter: list[str], runs: int) -> None:
    client = HarnessServiceClientSync(BITGN_URL)
    print("Connecting to BitGN:", client.status(StatusRequest()))

    res = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCHMARK_ID))
    benchmark_description = res.description
    print(
        f"Benchmark {res.benchmark_id}: {len(res.tasks)} task(s) listed, "
        f"{runs} run(s) → capturing environment snapshots\n"
    )

    stats: dict[str, int] = {}  # task_id → new snapshots saved

    for run_idx in range(1, runs + 1):
        print(f"{'─' * 60}  run {run_idx}/{runs}")

        try:
            run = client.start_run(StartRunRequest(
                name="collect_tasks snapshot",
                benchmark_id=BENCHMARK_ID,
                api_key=BITGN_API_KEY,
            ))
        except ConnectError as exc:
            print(f"{CLI_RED}FAILED to start run: {exc.message}{CLI_CLR}")
            continue

        try:
            for trial_id in run.trial_ids:
                print(f"  [{trial_id}] starting trial... ", end="", flush=True)

                try:
                    trial = client.start_trial(StartTrialRequest(trial_id=trial_id))
                except ConnectError as exc:
                    print(f"{CLI_RED}FAILED to start: {exc.message}{CLI_CLR}")
                    continue

                tid = trial.task_id
                if task_filter and tid not in task_filter:
                    print(f"skipped (filter)")
                    client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
                    continue

                instruction = trial.instruction
                vm = PcmRuntimeClientSync(trial.harness_url)

                env = capture_environment(vm)

                try:
                    client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
                except ConnectError as exc:
                    print(f"{CLI_YELLOW}(end_trial warning: {exc.message}){CLI_CLR} ", end="")

                is_new, path = save_snapshot(tid, instruction, env, benchmark_description)
                stats[tid] = stats.get(tid, 0) + (1 if is_new else 0)
                label = f"{CLI_GREEN}NEW{CLI_CLR}" if is_new else "dup"
                print(f"[{tid}] {label}  → {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path}")
        finally:
            client.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))

    print(f"\n{'=' * 60}")
    print("Summary – new unique snapshots saved:")
    for tid, count in sorted(stats.items()):
        existing = len(list((DATA_DIR / tid).glob("*.json"))) if (DATA_DIR / tid).exists() else 0
        print(f"  {tid}: +{count} new  ({existing} total)")


def main() -> None:
    args = sys.argv[1:]
    runs = 1
    task_filter: list[str] = []

    i = 0
    while i < len(args):
        if args[i] == "--runs" and i + 1 < len(args):
            runs = int(args[i + 1])
            i += 2
        else:
            task_filter.append(args[i])
            i += 1

    try:
        collect(task_filter, runs)
    except ConnectError as exc:
        print(f"{CLI_RED}{exc.code}: {exc.message}{CLI_CLR}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{CLI_RED}Interrupted{CLI_CLR}")
        sys.exit(1)


if __name__ == "__main__":
    main()
