"""Cooperative cancellation shared by Web, TUI, agents, and subprocesses."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Sequence


CancelCheck = Callable[[], bool] | None


class OperationCancelled(Exception):
    """Raised when the current user operation has been cancelled."""


def raise_if_cancelled(should_cancel: CancelCheck) -> None:
    if should_cancel is not None and should_cancel():
        raise OperationCancelled("用户已停止当前操作")


@contextmanager
def watch_cancellation(
    should_cancel: CancelCheck,
    on_cancel: Callable[[], None],
    *,
    interval: float = 0.05,
) -> Iterator[None]:
    """Call ``on_cancel`` from a daemon watcher as soon as cancellation is set.

    This is used to close a blocking HTTP response while its consumer is waiting
    for the first/next streamed chunk.
    """
    raise_if_cancelled(should_cancel)
    if should_cancel is None:
        yield
        return

    stopped = threading.Event()

    def watch() -> None:
        while not stopped.wait(interval):
            try:
                cancelled = should_cancel()
            except Exception:
                cancelled = False
            if cancelled:
                try:
                    on_cancel()
                except Exception:
                    pass
                return

    thread = threading.Thread(target=watch, name="flowchart-cancel-watch", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=interval * 2)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            killed = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if killed.returncode != 0 and proc.poll() is None:
                proc.kill()
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()


def run_cancellable_process(
    command: Sequence[str],
    *,
    should_cancel: CancelCheck = None,
    timeout: float | None = None,
    encoding: str | None = None,
    errors: str | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a captured subprocess and poll cancellation at a short interval."""
    raise_if_cancelled(should_cancel)
    proc = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding=encoding,
        errors=errors,
        cwd=cwd,
        start_new_session=(sys.platform != "win32"),
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if sys.platform == "win32"
            else 0
        ),
    )
    started = time.monotonic()
    while True:
        raise_timeout = False
        try:
            stdout, stderr = proc.communicate(timeout=0.05)
            return subprocess.CompletedProcess(
                list(command), proc.returncode, stdout=stdout, stderr=stderr
            )
        except subprocess.TimeoutExpired:
            if should_cancel is not None and should_cancel():
                _kill_process_tree(proc)
                proc.communicate()
                raise OperationCancelled("用户已停止当前操作")
            if timeout is not None and time.monotonic() - started >= timeout:
                raise_timeout = True
        if raise_timeout:
            _kill_process_tree(proc)
            stdout, stderr = proc.communicate()
            raise subprocess.TimeoutExpired(
                list(command), timeout, output=stdout, stderr=stderr
            )
