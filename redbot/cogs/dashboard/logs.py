"""In-memory capture of the bot's log output, for the owner-only log viewer.

Red writes its console output through `logging`, so the dashboard does not need
to tail a file or attach to a terminal: it installs a handler on the root logger
and keeps the last few thousand records in a ring buffer. The `/admin/logs` page
polls for anything newer than the sequence number it last saw.

Nothing here touches disk. The buffer is bounded and lives only as long as the
Dashboard cog is loaded, so it is a live console rather than a log archive - for
history, the bot's own log files are still the place to look.
"""

from __future__ import annotations

import itertools
import logging
import typing as t
from collections import deque

__all__ = ("DashboardLogHandler", "LEVELS")

# The capacity is a memory/usefulness trade: at ~400 bytes a record this is a
# couple of megabytes at worst, and holds a long enough tail to see what led up
# to an error someone is looking at.
DEFAULT_CAPACITY = 2500

# Offered as filter checkboxes on the page, coarsest last.
LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

MAX_MESSAGE_CHARS = 8000


class DashboardLogHandler(logging.Handler):
    """A bounded, thread-safe ring buffer of recent log records.

    `emit` runs on whatever thread logged - Red's asyncio loop, but also
    library threads - so it does the least work it can and must never raise:
    a handler that throws breaks logging for the whole process.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        super().__init__(level=logging.NOTSET)
        self.capacity = capacity
        # `deque` append/popleft are atomic under the GIL, which is all the
        # locking this needs - readers only ever take a snapshot.
        self.records: deque[dict[str, t.Any]] = deque(maxlen=capacity)
        self._counter = itertools.count(1)

    # ----------------------------------------------------------------- write

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if len(message) > MAX_MESSAGE_CHARS:
                message = message[:MAX_MESSAGE_CHARS] + "\n... (truncated)"

            traceback = None
            if record.exc_info:
                # `formatException` caches into `exc_text` on the record, so
                # this is not repeated work if another handler formats it too.
                traceback = self.format_exception(record)

            self.records.append(
                {
                    "seq": next(self._counter),
                    "time": record.created,
                    "level": record.levelname,
                    "levelno": record.levelno,
                    "logger": record.name,
                    "message": message,
                    "traceback": traceback,
                }
            )
        except Exception:  # noqa: BLE001
            # `handleError` respects `logging.raiseExceptions` and writes to
            # stderr rather than propagating into whatever was being logged.
            self.handleError(record)

    def format_exception(self, record: logging.LogRecord) -> str | None:
        try:
            if record.exc_text:
                return record.exc_text
            formatter = self.formatter or logging.Formatter()
            return formatter.formatException(record.exc_info)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ read

    @property
    def first_seq(self) -> int:
        """Sequence number of the oldest record still held, or 0 when empty."""
        try:
            return self.records[0]["seq"]
        except IndexError:
            return 0

    @property
    def last_seq(self) -> int:
        try:
            return self.records[-1]["seq"]
        except IndexError:
            return 0

    def read(
        self,
        after: int = 0,
        limit: int = 500,
        levels: t.Collection[str] | None = None,
        query: str | None = None,
    ) -> dict[str, t.Any]:
        """Records newer than `after`, oldest first.

        `gap` says the caller's cursor fell off the back of the buffer while it
        was away, so what it gets back is not contiguous with what it had.
        `truncated` says this response alone hit `limit`. The page shows either
        as a break rather than pretending nothing was missed.
        """
        # Snapshot first: the deque can be appended to while this runs.
        snapshot = list(self.records)

        wanted = {lvl.upper() for lvl in levels} if levels else None
        needle = (query or "").strip().lower() or None

        out = []
        for record in snapshot:
            if record["seq"] <= after:
                continue
            if wanted is not None and record["level"] not in wanted:
                continue
            if needle is not None and not self._matches(record, needle):
                continue
            out.append(record)

        truncated = len(out) > limit
        if truncated:
            # Keep the newest, which is what a console wants when it is behind.
            out = out[-limit:]

        first = snapshot[0]["seq"] if snapshot else 0
        return {
            "records": out,
            # Cursor advances to the newest record that exists, not the newest
            # one that matched, or a filter would make the client re-scan the
            # same records forever.
            "cursor": snapshot[-1]["seq"] if snapshot else after,
            # Only meaningful once the caller has a cursor: on a first load
            # there is nothing to have fallen behind.
            "gap": bool(after) and first > after + 1,
            "truncated": truncated,
            "buffered": len(snapshot),
            "capacity": self.capacity,
        }

    @staticmethod
    def _matches(record: dict[str, t.Any], needle: str) -> bool:
        if needle in record["message"].lower():
            return True
        if needle in record["logger"].lower():
            return True
        traceback = record["traceback"]
        return bool(traceback and needle in traceback.lower())

    def clear(self) -> None:
        self.records.clear()

    # ------------------------------------------------------------- lifecycle

    def install(self) -> None:
        """Attach to the root logger, below everything already configured.

        The handler's own level is NOTSET, so what arrives is whatever the root
        logger's level lets through - the same verbosity the console shows.
        """
        root = logging.getLogger()
        if self not in root.handlers:
            root.addHandler(self)

    def uninstall(self) -> None:
        logging.getLogger().removeHandler(self)
        self.clear()
