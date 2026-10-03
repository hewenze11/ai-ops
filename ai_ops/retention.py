"""Output retention and disk quota.

Raw stdout/stderr archives are the only unbounded data in the control database
(each stream is capped at 64 MiB, but nothing caps the number of tasks). This
module keeps disk use bounded without ever deleting evidence for work that is
still in flight:

* In-flight work is never pruned. Only *finalized* archives of *terminal* tasks
  are eligible, and a task whose result references archives is only terminal
  once it has settled.
* Two independent bounds are enforced: an age window (older archives go first)
  and a total byte budget (once exceeded, the oldest archives are evicted even
  if they are inside the age window).
* A upload-time guard refuses to accept new output bytes when the store is
  already over budget, so one runaway command cannot fill the disk before the
  pruner runs.

Nothing here rewrites or summarises command output; pruning is deletion of raw
bytes and is recorded in the audit log.
"""
import logging
import time

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB of stored output
DEFAULT_RETENTION_DAYS = 14
DEFAULT_KEEP_TASKS = 200
# States where the task has settled and its raw output is no longer needed for
# dispatch. "unknown" is deliberately excluded: an operator must resolve it and
# may still want the partial bytes until then.
TERMINAL_STATES = ("succeeded", "failed", "cancelled")

SCHEMA = ""  # no tables of its own; it governs output_archives/output_chunks


def policy_from_env(environ):
    def _int(name, default, low, high):
        raw = environ.get(name)
        if raw in (None, ""):
            return default
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(name + " must be an integer")
        if not low <= value <= high:
            raise ValueError(name + " out of range")
        return value

    return {
        "max_bytes": _int("AI_OPS_OUTPUT_MAX_BYTES", DEFAULT_MAX_BYTES, 0, 1 << 60),
        "retention_days": _int("AI_OPS_OUTPUT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, 0, 3650),
        "keep_tasks": _int("AI_OPS_OUTPUT_TASK_KEEP", DEFAULT_KEEP_TASKS, 0, 10_000_000),
    }


def _archive_rows(db):
    """Every finalized archive with its task's state, created_at and last update."""
    return db.execute(
        "SELECT a.task_id, a.stream, a.size, t.state AS task_state, "
        "t.created_at AS task_created, t.updated_at AS task_settled "
        "FROM output_archives a JOIN tasks t ON t.id=a.task_id WHERE a.finalized=1"
    ).fetchall()


def usage(db):
    total = db.execute("SELECT COALESCE(SUM(size),0) FROM output_archives").fetchone()[0]
    chunks = db.execute("SELECT COUNT(*) FROM output_chunks").fetchone()[0]
    finalized = db.execute("SELECT COUNT(*) FROM output_archives WHERE finalized=1").fetchone()[0]
    oldest = db.execute("SELECT MIN(t.created_at) FROM output_archives a JOIN tasks t ON t.id=a.task_id WHERE a.finalized=1").fetchone()[0]
    return {"total_bytes": int(total), "chunks": int(chunks),
            "archives": int(finalized), "oldest_archive_at": oldest}


def _delete_task_output(db, task_id):
    db.execute("DELETE FROM output_chunks WHERE task_id=?", (task_id,))
    db.execute("DELETE FROM output_archives WHERE task_id=?", (task_id,))


def prune(db, audit, policy, now=None):
    """Delete raw output that is past the age window or over the byte budget.

    Returns a summary. Never touches in-flight tasks or unresolved unknown ones.
    """
    now = time.time() if now is None else now
    rows = _archive_rows(db)
    # Group by task so a task's stdout and stderr are pruned together and the
    # "keep the newest N tasks" floor counts tasks, not streams.
    tasks = {}
    for row in rows:
        tasks.setdefault(row["task_id"], {"size": 0, "state": row["task_state"],
                                          "created": row["task_created"]})
        tasks[row["task_id"]]["size"] += int(row["size"])
    # Newest tasks first; the first `keep_tasks` eligible tasks are protected by
    # the floor regardless of age.
    ordered = sorted(tasks.items(), key=lambda kv: (kv[1]["created"], kv[0]), reverse=True)
    age_cutoff = now - policy["retention_days"] * 86400

    deleted_bytes = 0
    deleted_tasks = []

    kept = 0
    survivors = []
    for task_id, info in ordered:
        eligible = info["state"] in TERMINAL_STATES
        if not eligible:
            continue
        kept += 1
        if kept <= policy["keep_tasks"]:
            survivors.append((task_id, info))
            continue
        if info["created"] < age_cutoff:
            _delete_task_output(db, task_id)
            deleted_bytes += info["size"]
            deleted_tasks.append({"task_id": task_id, "reason": "age", "bytes": info["size"]})
        else:
            survivors.append((task_id, info))

    # Byte budget: evict oldest survivors first until under the cap.
    remaining = sum(info["size"] for _, info in survivors)
    for task_id, info in reversed(survivors):  # survivors is newest-first
        if remaining <= policy["max_bytes"]:
            break
        _delete_task_output(db, task_id)
        remaining -= info["size"]
        deleted_bytes += info["size"]
        deleted_tasks.append({"task_id": task_id, "reason": "budget", "bytes": info["size"]})

    if deleted_tasks:
        audit(db, "output.pruned", None, "service",
              {"tasks": len(deleted_tasks), "bytes": deleted_bytes,
               "reasons": sorted({t["reason"] for t in deleted_tasks})})
    return {"deleted_tasks": len(deleted_tasks), "deleted_bytes": deleted_bytes,
            "remaining_bytes": usage(db)["total_bytes"]}


def budget_guard(db, policy, extra_bytes, task_id):
    """Refuse new output bytes when the store is over budget.

    The task's own already-stored bytes do not count against it, so a command
    is never blocked simply because its own earlier chunks are large.
    """
    own = db.execute("SELECT COALESCE(SUM(size),0) FROM output_archives WHERE task_id=?", (task_id,)).fetchone()[0]
    total = db.execute("SELECT COALESCE(SUM(size),0) FROM output_archives").fetchone()[0]
    projected = int(total) - int(own) + extra_bytes
    return projected <= policy["max_bytes"], {"projected_bytes": projected, "max_bytes": policy["max_bytes"]}


def install_retention(app, transaction, audit, admin, policy):
    """Expose usage inspection and a manual prune to the administrator."""
    from fastapi import Depends

    @app.get("/api/v1/output/usage", dependencies=[Depends(admin)])
    def output_usage():
        with transaction() as db:
            return {**usage(db), "policy": policy}

    @app.post("/api/v1/output/prune", dependencies=[Depends(admin)])
    def output_prune():
        with transaction() as db:
            return prune(db, audit, policy)


def start_retention_worker(transaction, audit, policy, interval=300):
    """Run the pruner on a fixed cadence in a daemon thread."""
    import threading

    stop = threading.Event()

    def run():
        while not stop.is_set():
            try:
                with transaction() as db:
                    prune(db, audit, policy)
            except Exception as error:
                log.warning("Retention iteration failed: %s", type(error).__name__)
            stop.wait(interval)

    thread = threading.Thread(target=run, name="output-retention", daemon=True)
    thread.start()
    return stop, thread
