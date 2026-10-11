"""Run only The Connector API, without requiring Node or starting the UI."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from backend.network import lan_urls_for_host
from backend.app.user_settings import UserSettingsError, initialize_port_settings
from run import RunError, available_port, parse_port, resolve_host, service_url

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    hosts = parser.add_mutually_exclusive_group()
    hosts.add_argument("--host", help="Bind address (default: BACKEND_HOST, HOST, or 0.0.0.0)")
    hosts.add_argument("--local", action="store_true", help="Bind only to 127.0.0.1")
    parser.add_argument("--port", type=int, help="First listen port to try (default: settings.json connector_port, initialized from port_mapping.json)")
    parser.add_argument("--strict-port", action="store_true", help="Fail if the requested port is unavailable")
    parser.add_argument("--reload", action="store_true", help="Reload on backend code changes during development")
    parser.add_argument("--completion-no-database-access", action="store_true",
                        help="Serve completions without database access, saved memory, or response persistence")
    args = parser.parse_args()
    try:
        from dotenv import load_dotenv
        import uvicorn
    except ImportError as exc:
        parser.exit(1, "Backend dependencies are missing. Install them with:\n"
                    "  python -m pip install -r backend/requirements.txt\n" + str(exc) + "\n")
    os.chdir(ROOT)
    # Connectors read os.environ; Pydantic's env_file alone does not populate it.
    load_dotenv(ROOT / ".env", override=False)
    try:
        preferences = initialize_port_settings()
        requested_port = parse_port(args.port if args.port is not None else preferences["connector_port"], "backend port")
        family, host = resolve_host("127.0.0.1" if args.local else args.host or os.environ.get("BACKEND_HOST") or os.environ.get("HOST", "0.0.0.0"))
        port = available_port(requested_port, host=host, family=family)
        if args.strict_port and port != requested_port:
            raise RunError(f"Backend port {requested_port} is unavailable")
    except (RunError, UserSettingsError, OSError) as exc:
        parser.error(str(exc))
    if args.completion_no_database_access:
        os.environ["COMPLETION_NO_DATABASE_ACCESS"] = "true"
    if port != requested_port:
        print(f"[run] Backend port {requested_port} is busy; using {port}.", flush=True)
    url = service_url(host, port)
    print(f"The Connector backend: {url}", flush=True)
    print(f"API docs: {url}/docs", flush=True)
    for lan_url in lan_urls_for_host(host, port):
        print(f"LAN API: {lan_url}/docs", flush=True)
    print("Press Ctrl+C to stop. Frontend is not started.", flush=True)
    uvicorn.run("backend.app.main:app", host=host, port=port,
                reload=args.reload, reload_dirs=[str(ROOT / "backend")] if args.reload else None)


if __name__ == "__main__":
    main()
