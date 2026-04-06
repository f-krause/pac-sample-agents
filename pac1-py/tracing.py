"""
tracing.py – MLflow observability for PAC1 agent runs.

Stores traces in a local SQLite database at data/mlflow.db via MLflow's
SQL-based tracking. OpenAI calls are auto-traced by MLflow's built-in
OpenAI autologging.

Full agent runs (make run) are stored under the "full-runs" experiment.
Debug / single-task runs are stored under the "debug-runs" experiment so
full-run datasets stay clean for analysis.
"""

from contextlib import contextmanager
from pathlib import Path

import mlflow

_DATA_DIR = Path(__file__).parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

MLFLOW_DB = _DATA_DIR / "mlflow.db"
MLFLOW_TRACKING_URI = f"sqlite:///{MLFLOW_DB}"

_initialized = False


def init_tracing(*, debug: bool = False) -> None:
    """
    Configure MLflow with a SQLite tracking store and enable OpenAI autologging.

    Args:
        debug: If True, traces go to the "debug-runs" experiment;
               otherwise they go to "full-runs".
    """
    global _initialized
    if _initialized:
        return

    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)

    experiment_name = "debug-runs" if debug else "full-runs"
    mlflow.set_experiment(experiment_name)

    mlflow.openai.autolog()

    _initialized = True


@contextmanager
def trace_run(*, model_id: str, benchmark_id: str, task_count: int, debug: bool):
    """Top-level MLflow run wrapping an entire agent run."""
    with mlflow.start_run(run_name="agent-run") as run:
        mlflow.log_params(
            {
                "model_id": model_id,
                "benchmark_id": benchmark_id,
                "task_count": task_count,
                "debug": debug,
            }
        )
        yield run


@contextmanager
def trace_task(task_id: str, instruction: str):
    """Nested MLflow run wrapping a single task. OpenAI calls become children."""
    with mlflow.start_run(run_name=f"task:{task_id}", nested=True) as run:
        mlflow.log_params(
            {
                "task_id": task_id,
                "instruction": instruction[:250],
            }
        )
        yield run


def record_task_score(span, task_id: str, score: float, score_detail: list[str]) -> None:
    """Log scoring results as metrics/params on the current task run."""
    mlflow.log_metric("score", score)
    mlflow.log_param("score_detail", "\n".join(score_detail)[:250])
