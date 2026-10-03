import os
from pathlib import Path
import uvicorn


def main():
    # Read-only secret files are preferred to putting credentials in process args.
    secret_file = os.environ.get("AI_OPS_ADMIN_TOKEN_FILE")
    token = Path(secret_file).read_text().strip() if secret_file else os.environ.get("AI_OPS_ADMIN_TOKEN", "")
    from .app import create_app
    from .search import provider_from_env
    search_provider = provider_from_env(os.environ)
    app = create_app(os.environ.get("AI_OPS_DB", "data/control.db"), token,
                     os.environ.get("AI_OPS_MODEL", ""), admin_token_file=secret_file,
                     search_provider=search_provider)
    port = int(os.environ.get("AI_OPS_PORT", "8765"))
    background = []
    if os.environ.get("AI_OPS_SCHEDULER_ENABLED", "1") == "1":
        from .scheduler import start_scheduler
        background.append(start_scheduler(app.state.custom_tasks, port, token))
    if os.environ.get("AI_OPS_MODEL_KEY_FILE"):
        from .model_client import OpenAICompatible
        from .model_worker import start_model_workers
        from .memory_worker import start_memory_archiver
        client = OpenAICompatible(os.environ.get("AI_OPS_MODEL_BASE_URL", ""), os.environ["AI_OPS_MODEL_KEY_FILE"])
        background.append(start_model_workers(app.state.role_engine, client))
        # Let the role's own model author each day's memory archive, off the
        # context path. Disable with AI_OPS_MEMORY_ARCHIVER_ENABLED=0.
        if os.environ.get("AI_OPS_MEMORY_ARCHIVER_ENABLED", "1") == "1":
            background.append(start_memory_archiver(app, client))
    if os.environ.get("AI_OPS_SSH_CONNECTOR_ENABLED", "1") == "1":
        from .connector_ssh import start_connector_workers
        stop, threads = start_connector_workers(app, app.state.transaction, app.state.audit)
        background.append((stop, threads[0] if threads else None))
    if os.environ.get("AI_OPS_RETENTION_ENABLED", "1") == "1":
        from .retention import start_retention_worker
        stop, thread = start_retention_worker(app.state.transaction, app.state.audit, app.state.retention_policy)
        background.append((stop, thread))
    try:
        uvicorn.run(app, host=os.environ.get("AI_OPS_HOST", "127.0.0.1"), port=port, access_log=False)
    finally:
        for stop, _ in background:
            stop.set()
        for _, thread in background:
            thread.join(timeout=6)


if __name__ == "__main__":
    main()
