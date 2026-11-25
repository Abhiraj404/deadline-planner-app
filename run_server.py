"""Start one local Deadline Planner server, bound to 127.0.0.1.

Usage::

    python run_server.py                 # personal data in the data folder
    python run_server.py --demo --open   # synthetic demo data, opens the browser
    python run_server.py --init-only     # create or migrate the database, then exit

Only one server can use a data directory at a time. The server records its process
ID, port and a random shutdown token in ``.planner-server.json`` inside the data
directory. ``stop.ps1`` uses that file to stop only this application, never other
Python processes.
"""

import argparse
import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
HOST = '127.0.0.1'
STATE_FILE = '.planner-server.json'
LOCK_FILE = '.planner-server.lock'


def port_in_use(port: int) -> bool:
    """True if something already listens on this local port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((HOST, port)) == 0


def prepare_data_folder(root: Path) -> None:
    """Create the runtime folders and a .env from the template, keeping existing files."""
    for name in ('assessment_pdfs', 'planner_backups', 'planner_logs'):
        (root / name).mkdir(exist_ok=True)
    env = root / '.env'
    if not env.exists():
        env.write_bytes((APP_DIR / '.env.example').read_bytes())
        print(f'Created {env} from .env.example')


def open_when_ready(url: str, port: int) -> None:
    """Open the dashboard once the server accepts connections."""
    for _ in range(100):
        if port_in_use(port):
            webbrowser.open(url)
            return
        time.sleep(0.1)


def main(argv: list[str] | None = None) -> int:
    """Prepare the data folder, take the single-instance lock and run Uvicorn."""
    parser = argparse.ArgumentParser(description='Run the local Deadline Planner server.')
    parser.add_argument('--port', type=int, default=int(os.getenv('PLANNER_PORT', '0') or 0))
    parser.add_argument('--demo', action='store_true', help='use the synthetic demo data')
    parser.add_argument('--open', action='store_true', help='open the dashboard in a browser')
    parser.add_argument('--init-only', action='store_true', help='prepare the database and exit')
    args = parser.parse_args(argv)

    if args.demo:
        # These must be set before configuration.py is imported.
        os.environ['PLANNER_DATA_DIR'] = str(APP_DIR / 'demo-data')
        os.environ['PLANNER_DEMO'] = '1'
    port = args.port or (8001 if args.demo else 8000)
    if not 1024 <= port <= 65535:
        parser.error('Choose a port between 1024 and 65535')
    if not os.environ.get('PLANNER_DATA_DIR'):
        # One-time move of personal data out of the source folder (versions before 1.0.1).
        from data_location import migrate_legacy_data

        try:
            moved = migrate_legacy_data(APP_DIR)
        except RuntimeError as error:
            print(error, file=sys.stderr)
            return 5
        if moved:
            print(f'Moved {len(moved)} personal data items into {APP_DIR / "data"}')

    from configuration import ROOT, demo_mode

    if demo_mode():
        from demo_data import build_demo

        build_demo(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True)

    from local_files import try_lock

    # Held for the life of the process, so a second server for this folder refuses to start.
    lock = try_lock(ROOT / LOCK_FILE)
    if lock is None:
        print('Deadline Planner is already running for this data folder.', file=sys.stderr)
        return 3
    if args.init_only:
        prepare_data_folder(ROOT)
        import app

        app.initialize()
        print(f'Data folder ready: {ROOT}')
        return 0
    if port_in_use(port):
        print(f'Port {port} is already in use. Choose another with --port.', file=sys.stderr)
        return 4

    import uvicorn

    import app

    url = f'http://{HOST}:{port}/'
    server = uvicorn.Server(uvicorn.Config(app.app, host=HOST, port=port, log_level='warning'))
    token = secrets.token_urlsafe(32)
    app.app.state.shutdown_token = token
    app.app.state.request_shutdown = lambda: setattr(server, 'should_exit', True)
    state = ROOT / STATE_FILE
    state.write_text(
        json.dumps(
            {
                'pid': os.getpid(),
                'port': port,
                'url': url,
                'token': token,
                'app_dir': str(APP_DIR),
                'demo': demo_mode(),
                'started': time.time(),
            }
        ),
        encoding='utf-8',
    )
    print(f'Deadline Planner {"demo " if demo_mode() else ""}is running at {url}')
    print('Press Ctrl+C here, or run .\\stop.ps1, to stop it.')
    if args.open:
        threading.Thread(target=open_when_ready, args=(url, port), daemon=True).start()
    try:
        server.run()
    finally:
        state.unlink(missing_ok=True)
        lock.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
