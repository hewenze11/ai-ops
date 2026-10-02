import os
from pathlib import Path
import uvicorn


def main():
    # Read-only secret files are preferred to putting credentials in process args.
    secret_file = os.environ.get("AI_OPS_ADMIN_TOKEN_FILE")
    token = Path(secret_file).read_text().strip() if secret_file else os.environ.get("AI_OPS_ADMIN_TOKEN", "")
    from .app import create_app
    app = create_app(os.environ.get("AI_OPS_DB", "data/control.db"), token)
    uvicorn.run(app, host=os.environ.get("AI_OPS_HOST", "127.0.0.1"), port=int(os.environ.get("AI_OPS_PORT", "8765")), access_log=False)


if __name__ == "__main__":
    main()
