"""Run the API and scheduler together on one small Render web service.

This layout is intended for a free evaluator/demo deployment. A paid production deployment should
run the API and worker as separate services so the API can scale without duplicating the scheduler.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def main() -> int:
    # Render's Docker command is executed directly rather than through a shell. Keep the
    # idempotent release steps here so fresh databases are ready before the API starts.
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    subprocess.run([sys.executable, "-m", "legal_core.cli", "bootstrap"], check=True)
    # Seed the public demo with a small real-law baseline. The importer fetches the
    # current consolidated pages from the official Bureau of Experts portal, verifies
    # their structure/effective dates, then indexes only missing approved instruments.
    # A temporary upstream failure must not take the API offline; the next cold start
    # retries while an already active baseline is skipped without another download.
    baseline = subprocess.run(
        [sys.executable, "scripts/import_official_baseline.py", "--approve", "--if-missing"],
        check=False,
    )
    if baseline.returncode:
        print(f"official baseline import deferred (exit {baseline.returncode})", flush=True)

    port = os.environ.get("PORT", "10000")
    processes = [
        subprocess.Popen([sys.executable, "-m", "worker.main"]),
        subprocess.Popen(
            [
                "uvicorn",
                "api.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                port,
                "--workers",
                os.environ.get("WEB_CONCURRENCY", "1"),
                "--timeout-keep-alive",
                "15",
                "--no-server-header",
            ]
        ),
    ]

    stopping = False

    def stop(_signum: int | None = None, _frame: object | None = None) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while not stopping:
            for process in processes:
                code = process.poll()
                if code is not None:
                    stop()
                    return code
            time.sleep(0.5)
    finally:
        stop()
        deadline = time.monotonic() + 10
        for process in processes:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
