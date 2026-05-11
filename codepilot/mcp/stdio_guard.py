"""Protect the MCP stdio JSON-RPC channel from accidental I/O bleeding.

When the MCP server is launched with ``--transport stdio`` (the default for
embedded launchers such as ``codepilot chat -a claude``):

* ``sys.stdout`` is the JSON-RPC pipe to the client. Any text written there
  by a tool implementation — ``print``, ``click.echo``, ``rich.Console``,
  ``codepilot.core.output.echo`` — corrupts the protocol and causes both
  sides to silently block waiting for a sane message that never arrives.

* ``sys.stdin`` (fd 0) is the inbound JSON-RPC pipe. On Windows, child
  subprocesses spawned via ``subprocess.run(...)`` inherit fd 0 by default.
  When ``git``, ``ruff``, ``rg`` etc. inherit that pipe handle, they stall
  for several seconds before hitting their own internal timeout — turning
  fast tools (``inspect_project``, ``doctor``, ``explore``) into 15-second
  hangs that return empty results.

The MCP SDK keeps protocol I/O alive by accessing ``sys.stdout.buffer`` and
``sys.stdin.buffer`` inside ``stdio_server`` (see ``mcp/server/stdio.py``).
So we can safely:

* Replace the text-level ``sys.stdout`` with a guard whose ``.write`` goes
  to stderr but whose ``.buffer`` still resolves to the real binary stdout.
* Duplicate the real fd 0 to a saved descriptor (kept alive on the new
  ``sys.stdin``), then redirect fd 0 itself to ``DEVNULL`` so that any
  child subprocess inheriting fd 0 gets an immediately-EOF stream.
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import contextmanager
from typing import IO, Any, Iterator


class _StdoutProtocolGuard:
    """Text-IO shim that mimics ``sys.stdout`` but writes to ``sys.stderr``.

    The MCP SDK only needs ``sys.stdout.buffer`` to send JSON-RPC frames, so
    we expose the real binary stdout through ``.buffer`` while every text
    write is redirected to stderr where it cannot poison the protocol.
    """

    def __init__(self, original_stdout: IO[str], stderr: IO[str]) -> None:
        self._original = original_stdout
        self._stderr = stderr

    @property
    def buffer(self) -> Any:
        return self._original.buffer

    @property
    def encoding(self) -> str:
        return getattr(self._original, "encoding", "utf-8") or "utf-8"

    @property
    def errors(self) -> str | None:
        return getattr(self._original, "errors", None)

    def write(self, data: str) -> int:
        return self._stderr.write(data)

    def writelines(self, lines: Any) -> None:
        self._stderr.writelines(lines)

    def flush(self) -> None:
        self._stderr.flush()

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._stderr.fileno()

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def close(self) -> None:  # pragma: no cover - sys.stdout is not closed in practice
        self._stderr.flush()


def _isolate_child_stdin() -> tuple[int | None, Any]:
    """Redirect fd 0 to DEVNULL while preserving ``sys.stdin`` semantics.

    Returns ``(saved_real_fd, replacement_sys_stdin)`` so the context manager
    can both restore fd 0 on exit and install a Python-level ``sys.stdin``
    that reads from the real (saved) stdin descriptor. If fd 0 cannot be
    duplicated (e.g. running under a harness that has already shut stdin),
    returns ``(None, None)`` and the caller leaves ``sys.stdin`` alone.
    """
    try:
        saved_fd = os.dup(0)
    except (OSError, ValueError):
        return None, None

    try:
        devnull_fd = os.open(os.devnull, os.O_RDONLY)
    except OSError:
        os.close(saved_fd)
        return None, None

    try:
        os.dup2(devnull_fd, 0)
    finally:
        os.close(devnull_fd)

    try:
        raw = io.FileIO(saved_fd, mode="r", closefd=True)
        buffered = io.BufferedReader(raw)
        text_stream = io.TextIOWrapper(buffered, encoding="utf-8", errors="replace")
    except OSError:
        os.close(saved_fd)
        return None, None

    return saved_fd, text_stream


def _restore_fd0(saved_fd: int | None, replacement_stream: Any) -> None:
    if saved_fd is None:
        return
    try:
        os.dup2(saved_fd, 0)
    except OSError:
        pass
    # Closing the replacement stream also closes the FileIO around saved_fd,
    # which is fine because fd 0 now holds a dup of it.
    try:
        if replacement_stream is not None:
            replacement_stream.close()
    except Exception:
        pass


@contextmanager
def protect_stdio() -> Iterator[IO[str]]:
    """Install the stdio protocol guards for the duration of the block.

    Nested usage is a no-op: if the current ``sys.stdout`` is already a guard
    we yield it unchanged so we do not accidentally drop the original stream.
    """
    current_stdout = sys.stdout
    if isinstance(current_stdout, _StdoutProtocolGuard):
        yield current_stdout
        return

    guard = _StdoutProtocolGuard(current_stdout, sys.stderr)
    saved_fd, replacement_stdin = _isolate_child_stdin()
    original_stdin = sys.stdin
    sys.stdout = guard
    if replacement_stdin is not None:
        sys.stdin = replacement_stdin
    try:
        yield guard
    finally:
        sys.stdout = current_stdout
        if replacement_stdin is not None:
            sys.stdin = original_stdin
            _restore_fd0(saved_fd, replacement_stdin)
