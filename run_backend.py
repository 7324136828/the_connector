"""Run only The Connector API, without requiring Node or starting the UI."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8301, help="Listen port (default: 8301)")
    parser.add_argument("--reload", action="store_true", help="Reload on backend code changes during development")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        from dotenv import load_dotenv
        import uvicorn
    except ImportError as exc:
        parser.exit(1, "Backend dependencies are missing. Install them with:\n"
                    "  python -m pip install -r backend/requirements.txt\n" + str(exc) + "\n")
    os.chdir(ROOT)
    # Connectors read os.environ; Pydantic's env_file alone does not populate it.
    load_dotenv(ROOT / ".env", override=False)
    print(f"The Connector backend: http://{args.host}:{args.port}", flush=True)
    print(f"API docs: http://{args.host}:{args.port}/docs", flush=True)
    print("Press Ctrl+C to stop. Frontend is not started.", flush=True)
    uvicorn.run("backend.app.main:app", host=args.host, port=args.port,
                reload=args.reload, reload_dirs=[str(ROOT / "backend")] if args.reload else None)


if __name__ == "__main__":
    main()
