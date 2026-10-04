"""run_pool contract: reap-as-you-go, start-time ageing, isolation."""

from __future__ import annotations

import threading
import time

import pytest

from beatport_collector.pooling import run_pool


def test_every_item_reports_exactly_once():
    items = list(range(50))
    seen: list[int] = []
    run_pool(items, lambda i: i * 2, lambda i, r, a: seen.append(i), workers=4)
    assert sorted(seen) == items
    assert len(seen) == len(set(seen)), "an item was reported twice"


def test_queued_work_is_never_marked_stuck():
    """Regression: ageing from submit time convicted 1,477 innocent files.

    More items than workers, each slower than the watchdog budget, must all
    complete. Only work that has actually *started* can age out.
    """
    items = list(range(12))
    abandoned: list[int] = []
    done: list[int] = []

    def slow(i: int) -> int:
        time.sleep(0.25)
        return i

    def on_done(i: int, r: int | None, was_abandoned: bool) -> None:
        (abandoned if was_abandoned else done).append(i)

    # stuck_after sits between per-item runtime (0.25s) and the max queue
    # wait under submit-time ageing (~1.25s): the old bug trips it, correct
    # start-time ageing never does.
    run_pool(items, slow, on_done, workers=2, stuck_after=1.0)
    assert not abandoned
    assert sorted(done) == items


def test_genuinely_wedged_task_is_abandoned():
    """A task that never returns is dropped, and the batch still finishes."""
    gate = threading.Event()
    finished: list[str] = []
    abandoned: list[str] = []

    def work(name: str) -> str:
        if name == "hang":
            gate.wait(30)
        return name

    def on_done(name: str, r: str | None, was_abandoned: bool) -> None:
        (abandoned if was_abandoned else finished).append(name)

    items = ["hang", "a", "b", "c"]
    t = threading.Thread(
        target=run_pool,
        args=(items, work, on_done),
        kwargs={"workers": 2, "stuck_after": 0.3},
        daemon=True,
    )
    t.start()
    time.sleep(1.5)
    gate.set()
    t.join(10)

    assert "hang" in abandoned, "wedged task was not abandoned"
    assert set(finished) == {"a", "b", "c"}, f"batch did not finish: {finished}"


def test_one_raising_item_does_not_kill_the_batch():
    def work(i: int) -> int:
        if i % 2:
            raise ValueError(f"boom {i}")
        return i

    seen: list[tuple[int, int | None, bool]] = []
    run_pool(range(10), work, lambda i, r, a: seen.append((i, r, a)), workers=3)
    assert len(seen) == 10
    assert {i for i, _, _ in seen} == set(range(10))
    assert all(a is False for _, _, a in seen), "a raise is not an abandonment"
    assert all(r is None for i, r, _ in seen if i % 2)


def test_results_carry_through():
    got: dict[int, int | None] = {}
    run_pool(
        range(5),
        lambda i: i + 100,
        lambda i, r, _a: got.__setitem__(i, r),
        workers=2,
    )
    assert got == {i: i + 100 for i in range(5)}


def test_zero_workers_does_not_deadlock():
    got: list[int | None] = []
    run_pool([1, 2], lambda i: i, lambda i, r, _a: got.append(r), workers=0)
    assert sorted(x for x in got if x is not None) == [1, 2]


def test_zero_workers_runs_on_a_single_worker():
    """workers=0 clamps to one thread — a second worker must never appear.

    Mutation catch: max(1, workers) -> None/max(2, workers) both survived.
    """
    active = 0
    peak = 0
    lock = threading.Lock()
    gate = threading.Event()

    def work(i: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        gate.wait(5)  # hold the worker while a second item is pending
        with lock:
            active -= 1
        return i

    done: list[int] = []
    t = threading.Thread(
        target=run_pool,
        args=([1, 2], work, lambda i, r, _a: done.append(i)),
        kwargs={"workers": 0},
        daemon=True,
    )
    t.start()
    time.sleep(1.0)  # both items are queued; a 2nd worker would start item 2
    assert peak == 1, f"expected a single worker, saw concurrency {peak}"
    gate.set()
    t.join(10)
    assert sorted(done) == [1, 2]


def test_results_stream_instead_of_arriving_at_the_end():
    """Reaping happens per finished task, not once the whole batch is done.

    Mutation catch: dropping return_when=FIRST_COMPLETED survived.
    """
    first_at: list[float] = []
    start = time.monotonic()

    def slow(i: int) -> int:
        time.sleep(0.3)
        return i

    def on_done(i: int, r: int | None, _a: bool) -> None:
        if not first_at:
            first_at.append(time.monotonic())

    # 8 x 0.3s over 2 workers drains in ~1.2s; streaming reports at ~0.3s.
    run_pool(range(8), slow, on_done, workers=2)
    total = time.monotonic() - start
    assert first_at
    assert first_at[0] - start < total * 0.6


@pytest.mark.parametrize("workers", [1, 3, 8])
def test_worker_count_does_not_change_outcome(workers):
    items = list(range(20))
    got: list[int] = []
    run_pool(
        items,
        lambda i: i,
        lambda i, r, _a: got.append(r) if r is not None else None,
        workers=workers,
    )
    assert sorted(got) == items


def test_stop_between_sweeps_defers_rest():
    """The post-abandon stop check (not just the post-reap one) bites.

    stop_when False at the first poll, True at the second: the single item
    still completes (exactly-once holds) but the pool exits via the second
    stop branch.
    """
    answers = iter([False, True])
    done: list[int] = []
    run_pool(
        [1],
        lambda i: i,
        lambda i, r, _a: done.append(i),
        workers=1,
        stop_when=lambda: next(answers, True),
    )
    assert done == [1]
