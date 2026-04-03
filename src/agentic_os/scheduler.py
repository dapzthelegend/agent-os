"""
Background scheduler — a generic, job-agnostic dispatch engine.

The scheduler knows nothing about what jobs exist or when they should run.
Jobs subscribe to it via `register()`.  Each job supplies its own callable
that returns the *datetime* of its next run; the engine just dispatches.

Usage
-----
    scheduler = BackgroundScheduler()
    scheduler.register(job)          # from jobs.py
    scheduler.start()                # spawns daemon thread
    scheduler.stop()                 # signals thread to exit cleanly

See `jobs.py` for all job definitions and schedules.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# How often the dispatch loop ticks.  Shorter = more responsive starts; the
# loop goes back to sleep immediately if no job is due.
_TICK_SECONDS = 5


@dataclass
class ScheduledJob:
    """
    A single registered job.

    Parameters
    ----------
    name:
        Human-readable identifier used in logs.
    func:
        Callable that performs the work.  Must be safe to call from a
        background thread.  Return value is logged at DEBUG level.
    next_run_at:
        Zero-argument callable that returns a *timezone-aware* datetime
        representing when this job should next run.  Called once after
        every successful (or failed) execution to reschedule.
        On first registration the job's initial `_due_at` is set by
        calling this function once.
    run_immediately:
        If True (default), the job runs on the first tick rather than
        waiting until `next_run_at()` from now.
    """
    name: str
    func: Callable[[], Any]
    next_run_at: Callable[[], datetime]
    run_immediately: bool = True
    _due_at: datetime = field(init=False)

    def __post_init__(self) -> None:
        if self.run_immediately:
            self._due_at = datetime.now(timezone.utc)
        else:
            self._due_at = self.next_run_at()


class BackgroundScheduler:
    """
    Thread-based dispatcher.  Completely unaware of any concrete job logic.
    """

    def __init__(self) -> None:
        self._jobs: list[ScheduledJob] = []
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def register(self, job: ScheduledJob) -> None:
        """Subscribe a job.  Safe to call before or after start()."""
        self._jobs.append(job)
        log.info("scheduler: registered job '%s' (due %s)", job.name, job._due_at.isoformat())

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="agentic-os-scheduler",
            daemon=True,
        )
        self._thread.start()
        log.info("scheduler: started (%d job(s) registered)", len(self._jobs))

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        log.info("scheduler: stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now(timezone.utc)
            for job in list(self._jobs):
                if now >= job._due_at:
                    self._run(job)
            self._stop_event.wait(timeout=_TICK_SECONDS)

    def _run(self, job: ScheduledJob) -> None:
        log.info("scheduler: running '%s'", job.name)
        try:
            result = job.func()
            log.debug("scheduler: '%s' result: %s", job.name, result)
        except Exception as exc:
            log.error("scheduler: '%s' failed: %s", job.name, exc, exc_info=True)
        finally:
            job._due_at = job.next_run_at()
            log.debug("scheduler: '%s' next run at %s", job.name, job._due_at.isoformat())
