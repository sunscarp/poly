#!/usr/bin/env python3
"""
Launcher — starts the trading bot loop in a background thread and serves the dashboard.

Usage:
    python run.py              # bot + dashboard
    python run.py --dashboard  # dashboard only
    python run.py --bot        # bot only
"""

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PORT = int(os.environ.get("PORT", os.environ.get("DASHBOARD_PORT", 8081)))


def main():
    args = sys.argv[1:]
    dash_only = "--dashboard" in args
    bot_only = "--bot" in args

    print(f"\n{'='*50}")
    print(f"  WEATHER NO PAPER TRADER")
    print(f"{'='*50}")

    if dash_only:
        print(f"  Mode: Dashboard only")
        print(f"{'='*50}\n")
        import dashboard_server
        dashboard_server.main()
        return

    if bot_only:
        print(f"  Mode: Bot only")
        print(f"{'='*50}\n")
        from orchestrator import cmd_run
        cmd_run()
        return

    # Both: bot in background, dashboard in foreground
    print(f"  Mode: Bot + Dashboard")
    print(f"  Dashboard: http://localhost:{PORT}")
    print(f"{'='*50}\n")

    from orchestrator import cmd_run, _orchestrator_state
    import dashboard_server

    dashboard_server.inject_orchestrator_state(_orchestrator_state)

    bot_thread = threading.Thread(target=cmd_run, daemon=True, name="bot")
    bot_thread.start()

    time.sleep(2)
    try:
        webbrowser = __import__("webbrowser")
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass

    try:
        dashboard_server.main()
    except KeyboardInterrupt:
        print("\n  Stopping...")
        sys.exit(0)


if __name__ == "__main__":
    main()
