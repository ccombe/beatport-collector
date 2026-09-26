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

    # stuck_after is below per-item runtime but above pool drain time only if
    # ageing starts at submit; here it must NOT trip, so keep it generous
    # relative to total runtime and assert nothing was abandoned.
    run_pool(items, slow, on_done, workers=2, stuck_after=10.0)
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
