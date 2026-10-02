import os
from pathlib import Path
import uvicorn


def main():
    # Read-only secret files are preferred to putting credentials in process args.
    secret_file = os.environ.get("AI_OPS_ADMIN_TOKEN_FILE")
    token = Path(secret_file).read_text().strip() if secret_file else os.environ.get("AI_OPS_ADMIN_TOKEN", "")
    from .app import create_app
    app = create_app(os.environ.get("AI_OPS_DB", "data/control.db"), token)
    port = int(os.environ.get("AI_OPS_PORT", "8765"))
    scheduler = None
    if os.environ.get("AI_OPS_SCHEDULER_ENABLED", "1") == "1":
        from .scheduler import start_scheduler
        scheduler = start_scheduler(app.state.custom_tasks, port, token)
    try:
        uvicorn.run(app, host=os.environ.get("AI_OPS_HOST", "127.0.0.1"), port=port, access_log=False)
    finally:
        if scheduler is not None:
            stop, thread = scheduler
            stop.set()
            thread.join(timeout=6)


if __name__ == "__main__":
    main()
