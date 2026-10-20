"""Run the API and worker in one Railway service (small single-instance deployments only).

Uploads are stored in PostgreSQL, so the API and the worker no longer need a shared volume. For
1,000+ users deploy them as separate services instead (see deploy/railway/README.md): API replicas
can then scale without multiplying the scheduler.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def main() -> int:
    port = os.environ.get("PORT", "8000")
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
                os.environ.get("WEB_CONCURRENCY", "2"),
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
            remaining = max(0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
