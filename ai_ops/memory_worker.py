"""Background worker: let the role's OWN model author each day's memory archive.

Why this exists (user, 2026-10-03): archiving memory is a normal, expected token
cost, so it should use the SAME model the user configured for the role rather
than a fixed deterministic algorithm. This produces higher-quality "what
happened" memory than head/tail trimming.

Non-negotiables:
  * NEVER on the context path. A turn must never pay a surprise model call just
    to assemble memory. build_day only QUEUES a job; this worker runs it.
  * NEVER lose memory. build_day already wrote deterministic compressed/summary
    forms, so if the model is disabled, over budget, or fails, the day still has
    usable memory. We just record a fallback state and stop retrying on hard
    errors (a content/format problem will not fix itself by retrying forever).
  * No tools, no authority. The archive prompt is a closed summarisation task;
    the model cannot execute anything here.
  * The model key is read only at the HTTP boundary by the client, never here.
"""
import logging
import threading

log = logging.getLogger(__name__)

# Give up after this many attempts and keep the deterministic forms.
MAX_ATTEMPTS = 3
# Errors that will not improve by retrying; go straight to fallback.
HARD_ERRORS = ("MODEL_INVALID_RESPONSE", "MODEL_EMPTY_RESPONSE",
               "MODEL_INCOMPLETE_RESPONSE", "MODEL_RESPONSE_TOO_LARGE",
               "MODEL_REDIRECT_REJECTED", "MODEL_CREDENTIAL_EMPTY")


def archive_once(app, client):
    """Process one pending archive job. Returns True if a job was handled."""
    from . import memory as memory_module
    from .model_client import ModelFailure
    with app.state.transaction() as db:
        jobs = memory_module.claim_archive_jobs(db, limit=1)
        if not jobs:
            return False
        role_id, day = jobs[0]
        row = db.execute("SELECT * FROM role_memory WHERE role_id=? AND day=?", (role_id, day)).fetchone()
        if row is None or not (row["full_text"] or "").strip():
            memory_module._close_archive(db, role_id, day, memory_module.ARCHIVE_DONE, None)
            return True
        full_text = row["full_text"]
        attempts = db.execute("SELECT attempts FROM memory_archive WHERE role_id=? AND day=?",
                              (role_id, day)).fetchone()["attempts"]
        db.execute("UPDATE memory_archive SET attempts=attempts+1, updated_at=? WHERE role_id=? AND day=?",
                   (__import__("time").time(), role_id, day))

    body = {
        "model": app.state.role_engine.default_model or "",
        "messages": memory_module.build_archive_messages(full_text),
        "max_tokens": 900,
    }
    if not body["model"]:
        with app.state.transaction() as db:
            memory_module.mark_archive_fallback(db, role_id, day, "MODEL_NOT_CONFIGURED")
        return True
    try:
        response = client.complete(body)
        parsed = memory_module.parse_archive_reply((response.get("message") or {}).get("content"))
        if parsed is None:
            raise ModelFailure("MODEL_INVALID_RESPONSE")
        compressed, summary = parsed
    except ModelFailure as error:
        code = str(error)
        hard = code in HARD_ERRORS or attempts + 1 >= MAX_ATTEMPTS
        with app.state.transaction() as db:
            if hard:
                memory_module.mark_archive_fallback(db, role_id, day, code)
            # else: leave pending, retry on a later pass.
        return True
    except Exception as error:  # network/transport: retry, then fallback
        with app.state.transaction() as db:
            if attempts + 1 >= MAX_ATTEMPTS:
                memory_module.mark_archive_fallback(db, role_id, day, type(error).__name__)
        return True
    with app.state.transaction() as db:
        memory_module.apply_model_archive(db, role_id, day, compressed, summary)
        app.state.audit(db, "memory.archived_by_model", role_id, "service",
                        {"day": day, "chars": len(compressed)})
    return True


def start_memory_archiver(app, client, poll_seconds=2.0):
    """Bounded single-thread archiver. Archive calls are cheap and occasional."""
    stop = threading.Event()
    if client is None:
        return stop, None

    def run():
        while not stop.is_set():
            try:
                handled = archive_once(app, client)
            except Exception as error:
                log.warning("Memory archiver failed: %s", type(error).__name__)
                handled = False
            stop.wait(0.2 if handled else poll_seconds)

    thread = threading.Thread(target=run, name="memory-archiver", daemon=True)
    thread.start()
    return stop, thread
