"""Bounded thread pool with a start-time watchdog.

Both batch phases (dry-run search, apply) need the same three properties,
and getting any of them wrong wedges a long run:

- tasks are **reaped as they finish**, so progress streams instead of
  arriving in submission order at the end;
- ageing starts when a task actually **begins running**, not when it is
  submitted — with hundreds of items queued behind a few workers, submit
  time would convict almost every one of them as "stuck" before it ran;
- a genuinely wedged task cannot block the batch forever, so anything
  still running past ``stuck_after`` is abandoned and reported.

Reporting happens on the calling thread, so callers need no locking.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

logger = logging.getLogger(__name__)

#: Abandon a task that has been running longer than this (seconds).
STUCK_AFTER = 300.0
#: How often the pool sweeps for finished/overdue tasks.
POLL_SECONDS = 30.0
#: Floor on the sweep interval, so a tiny budget cannot spin the CPU.
MIN_POLL_SECONDS = 0.05

#: ``on_done(item, result, abandoned)`` — ``abandoned`` marks a task that was
#: dropped mid-flight rather than completing.
type OnDone[T, R] = Callable[[T, R | None, bool], None]


def _defer_rest(pending: dict, started: dict[int, float]) -> None:
    """Report what we are NOT doing, so a resumed run picks it up.

    Deferred items get no ``on_done`` call: they were never started, so
    calling it would log them as finished work.
    """
    if pending:
        logger.warning(
            "Stopping early: %d item(s) deferred to the next run", len(pending)
        )
    pending.clear()
    started.clear()


def run_pool[T, R](
    items: Iterable[T],
    work: Callable[[T], R],
    on_done: OnDone[T, R],
    *,
    workers: int,
    describe: Callable[[T], str] = str,
    stuck_after: float = STUCK_AFTER,
    stop_when: Callable[[], bool] | None = None,
) -> None:
    """Run ``work`` over ``items`` in a bounded pool, calling ``on_done`` per item.

    Every item produces exactly one ``on_done`` call, in one of three ways:
    it completed (``result`` set, ``abandoned`` False), it raised
    (``result`` None, ``abandoned`` False), or it overran ``stuck_after``
    and was abandoned (``result`` None, ``abandoned`` True). A task that
    raises is logged and never propagates: one bad file must not end a
    batch of thousands.

    ``stop_when`` is polled between sweeps; when it returns True the pool
    stops taking on new work (a circuit breaker for systematic failures).
    Nothing already running is dropped silently — it is simply left
    unstarted, and the caller resumes it on the next run.
    """
    started: dict[int, float] = {}

    def _run(item: T) -> R:
        # Stamp on entry, so queued work never looks stuck.
        started[id(item)] = time.monotonic()
        return work(item)

    # Sweep often enough that a task is dropped near its budget, not up to
    # POLL_SECONDS late. With the production budget (300s) this is 30s.
    poll = max(MIN_POLL_SECONDS, min(POLL_SECONDS, stuck_after))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pending = {pool.submit(_run, item): item for item in items}
        while pending:
            finished, _ = wait(set(pending), timeout=poll, return_when=FIRST_COMPLETED)
            now = time.monotonic()
            for fut in finished:
                item = pending.pop(fut)
                started.pop(id(item), None)
                try:
                    result: R | None = fut.result()
                except Exception as e:  # noqa: BLE001 - one bad item must not kill the batch
                    logger.warning("Worker failed for %s: %s", describe(item), e)
                    on_done(item, None, False)
                else:
                    on_done(item, result, False)
                # Check after every reap: a fast worker can finish the whole
                # batch between sweeps, and the breaker must still bite.
                if stop_when is not None and stop_when():
                    _defer_rest(pending, started)
                    return
            for fut, item in list(pending.items()):
                birth = started.get(id(item))
                if birth is None or now - birth <= stuck_after:
                    continue
                logger.warning(
                    "Abandoning after %.0fs: %s", now - birth, describe(item)
                )
                pending.pop(fut)
                started.pop(id(item), None)
                on_done(item, None, True)
            if stop_when is not None and stop_when():
                _defer_rest(pending, started)
                return


def nothing[T, R](item: T, result: R | None, abandoned: bool) -> None:
    """No-op ``on_done``, for callers that only want the side effects."""
    return
