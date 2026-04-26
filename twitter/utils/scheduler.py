"""
TaskScheduler — lightweight cron-style scheduler built on threading.

Design goals:
  - No external dependencies (APScheduler, Celery, etc.)
  - Each task runs in its own daemon thread so the main process can exit cleanly
  - Tasks can be added/removed at runtime
  - Full error isolation: exceptions in one task never crash others
  - Supports one-shot, interval, and daily-at scheduling
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class Task:
    name: str
    func: Callable[..., Any]
    args: tuple = field(default_factory=tuple)
    kwargs: Dict[str, Any] = field(default_factory=dict)

    # Scheduling
    interval_seconds: Optional[float] = None    # run every N seconds
    daily_at: Optional[str] = None              # "HH:MM" UTC
    run_once: bool = False                      # one-shot

    # State
    last_run: Optional[float] = None
    next_run: float = field(default_factory=time.time)
    run_count: int = 0
    error_count: int = 0
    enabled: bool = True


class TaskScheduler:
    """
    Simple interval/daily scheduler using daemon threads.

    Usage
    -----
    scheduler = TaskScheduler()
    scheduler.add_interval("fetch_prices", fetch_prices_fn, interval=60)
    scheduler.add_daily("morning_digest", digest_fn, at="08:00")
    scheduler.start()
    ...
    scheduler.stop()
    """

    def __init__(self, tick: float = 1.0):
        self._tick = tick
        self._tasks: Dict[str, Task] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def add_interval(
        self,
        name: str,
        func: Callable,
        interval: float,
        args: tuple = (),
        kwargs: Optional[Dict] = None,
        run_immediately: bool = True,
        enabled: bool = True,
    ) -> None:
        """Register a task to run every `interval` seconds."""
        next_run = time.time() if run_immediately else time.time() + interval
        task = Task(
            name=name,
            func=func,
            args=args,
            kwargs=kwargs or {},
            interval_seconds=interval,
            next_run=next_run,
            enabled=enabled,
        )
        with self._lock:
            self._tasks[name] = task
        log.info("Scheduled interval task '%s' every %.0fs", name, interval)

    def add_daily(
        self,
        name: str,
        func: Callable,
        at: str,
        args: tuple = (),
        kwargs: Optional[Dict] = None,
        enabled: bool = True,
    ) -> None:
        """Register a task to run once daily at `at` UTC time (e.g. '08:00')."""
        task = Task(
            name=name,
            func=func,
            args=args,
            kwargs=kwargs or {},
            daily_at=at,
            next_run=self._next_daily_ts(at),
            enabled=enabled,
        )
        with self._lock:
            self._tasks[name] = task
        log.info("Scheduled daily task '%s' at %s UTC", name, at)

    def add_once(
        self,
        name: str,
        func: Callable,
        delay: float = 0.0,
        args: tuple = (),
        kwargs: Optional[Dict] = None,
    ) -> None:
        """Run a task once after `delay` seconds."""
        task = Task(
            name=name,
            func=func,
            args=args,
            kwargs=kwargs or {},
            run_once=True,
            next_run=time.time() + delay,
        )
        with self._lock:
            self._tasks[name] = task

    def remove(self, name: str) -> bool:
        with self._lock:
            return bool(self._tasks.pop(name, None))

    def enable(self, name: str) -> None:
        with self._lock:
            if name in self._tasks:
                self._tasks[name].enabled = True

    def disable(self, name: str) -> None:
        with self._lock:
            if name in self._tasks:
                self._tasks[name].enabled = False

    def get_status(self) -> List[Dict]:
        with self._lock:
            return [
                {
                    "name": t.name,
                    "enabled": t.enabled,
                    "run_count": t.run_count,
                    "error_count": t.error_count,
                    "last_run": datetime.utcfromtimestamp(t.last_run).isoformat()
                    if t.last_run
                    else None,
                    "next_run": datetime.utcfromtimestamp(t.next_run).isoformat(),
                    "interval_seconds": t.interval_seconds,
                    "daily_at": t.daily_at,
                }
                for t in self._tasks.values()
            ]

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            log.warning("Scheduler already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="TaskScheduler", daemon=True
        )
        self._thread.start()
        log.info("TaskScheduler started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        log.info("TaskScheduler stopped")

    def run_forever(self) -> None:
        """Block the calling thread until stop() is called from another thread."""
        self.start()
        try:
            while not self._stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt — stopping scheduler")
            self.stop()

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            now = time.time()
            to_remove = []

            with self._lock:
                tasks = list(self._tasks.values())

            for task in tasks:
                if not task.enabled:
                    continue
                if now < task.next_run:
                    continue

                # Dispatch to worker thread so scheduler loop never blocks
                worker = threading.Thread(
                    target=self._run_task, args=(task,), daemon=True
                )
                worker.start()

                if task.run_once:
                    to_remove.append(task.name)

            with self._lock:
                for name in to_remove:
                    self._tasks.pop(name, None)

            time.sleep(self._tick)

    def _run_task(self, task: Task) -> None:
        log.debug("Running task '%s'", task.name)
        try:
            task.func(*task.args, **task.kwargs)
            task.run_count += 1
            task.last_run = time.time()
        except Exception as exc:
            task.error_count += 1
            log.exception("Task '%s' raised an exception: %s", task.name, exc)

        # Schedule next run
        if task.interval_seconds:
            task.next_run = time.time() + task.interval_seconds
        elif task.daily_at:
            task.next_run = self._next_daily_ts(task.daily_at)

    @staticmethod
    def _next_daily_ts(at: str) -> float:
        """Return the next UTC epoch for HH:MM daily schedule."""
        hh, mm = map(int, at.split(":"))
        now = datetime.utcnow()
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.timestamp()
