"""TaskTracker: state machine, on-disk history, per-person queries, cancel, and expiry of unadmitted runs."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hermes_duplex.providers.base import ModelChoice
from hermes_duplex.tasks import Adoption, IllegalTransitionError, Task, TaskState, TaskTracker


class _RecordingStop:
    """A ``stop(profile, run_id)`` stand-in that records its calls and can fail."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._error = error

    async def __call__(self, profile: str, run_id: str) -> None:
        self.calls.append((profile, run_id))
        if self._error is not None:
            raise self._error


def _history(tracker: TaskTracker) -> list[dict[str, Any]]:
    return [json.loads(line) for line in tracker.history_path.read_text(encoding="utf-8").splitlines()]


async def _create(
    tracker: TaskTracker, person: str = "rob", profile: str = "general", text: str = "hi"
) -> Task:
    return await tracker.create(person=person, profile=profile, effort="medium", model=None, text=text)


@pytest.fixture
async def tracker(tmp_path: Path) -> AsyncIterator[TaskTracker]:
    tracker = TaskTracker(tmp_path, unadmitted_timeout_s=0.05)
    yield tracker
    await tracker.close()


async def test_create_numbers_tasks_from_one_and_starts_in_routing(tracker: TaskTracker) -> None:
    first = await tracker.create(
        person="rob",
        profile="stock-picker",
        effort="high",
        model=ModelChoice("openai", "gpt-x"),
        text="how's my portfolio",
        delegation_id="del_1",
    )
    second = await _create(tracker)

    assert (first.number, second.number) == (1, 2)
    assert first.state == "routing"
    assert first.run_id is None
    assert first.uuid != second.uuid
    assert first.created_at == first.updated_at


@pytest.mark.parametrize(
    "path",
    [
        ["queued", "running", "completed"],
        ["running", "failed"],
        ["running", "cancelled"],
        ["running", "unknown"],
        ["queued", "unknown"],
        ["queued", "cancelled"],
        ["failed"],
    ],
)
async def test_legal_transitions(tracker: TaskTracker, path: list[TaskState]) -> None:
    task = await _create(tracker)
    for state in path:
        task = await tracker.transition(task.number, state, run_id="run_1")
    assert task.state == path[-1]
    assert tracker.get(task.number) == task


@pytest.mark.parametrize(
    ("path", "illegal"),
    [
        ([], "completed"),
        ([], "unknown"),
        (["running"], "queued"),
        (["running"], "routing"),
        (["running", "completed"], "failed"),
        (["running", "cancelled"], "running"),
    ],
)
async def test_illegal_transitions_are_rejected(
    tracker: TaskTracker, path: list[TaskState], illegal: TaskState
) -> None:
    task = await _create(tracker)
    for state in path:
        task = await tracker.transition(task.number, state, run_id="run_1")
    with pytest.raises(IllegalTransitionError):
        await tracker.transition(task.number, illegal)
    assert tracker.get(task.number) == task


async def test_repeating_a_terminal_state_is_a_no_op(tracker: TaskTracker) -> None:
    task = await _create(tracker)
    await tracker.transition(task.number, "running", run_id="run_1")
    cancelled = await tracker.transition(task.number, "cancelled")

    assert await tracker.transition(task.number, "cancelled") == cancelled
    assert [record["state"] for record in _history(tracker)] == ["routing", "running", "cancelled"]


async def test_transition_keeps_run_id_unless_a_new_one_is_given(tracker: TaskTracker) -> None:
    task = await _create(tracker)
    await tracker.transition(task.number, "queued", run_id="run_1")
    running = await tracker.transition(task.number, "running")
    assert running.run_id == "run_1"


async def test_history_is_one_json_object_per_transition_in_order(tmp_path: Path) -> None:
    tracker = TaskTracker(tmp_path / "home")
    task = await tracker.create(
        person="rob",
        profile="stock-picker",
        effort="high",
        model=ModelChoice("openai", "gpt-x"),
        text="how's my portfolio",
        delegation_id="del_1",
        call_id=None,
    )
    await tracker.transition(task.number, "queued", run_id="run_1")
    await tracker.transition(task.number, "running")
    await tracker.transition(task.number, "completed")

    assert tracker.history_path == tmp_path / "home" / "duplex" / "tasks.jsonl"
    records = _history(tracker)
    assert [record["state"] for record in records] == ["routing", "queued", "running", "completed"]
    assert records[0] == {
        "task": 1,
        "uuid": task.uuid,
        "person": "rob",
        "profile": "stock-picker",
        "effort": "high",
        "model": {"provider": "openai", "model": "gpt-x"},
        "text": "how's my portfolio",
        "run_id": None,
        "state": "routing",
        "created_at": task.created_at.isoformat(),
        "updated_at": task.created_at.isoformat(),
        "delegation_id": "del_1",
        "call_id": None,
    }
    assert records[-1]["run_id"] == "run_1"
    await tracker.close()


async def test_overlapping_transitions_are_written_in_transition_order(tracker: TaskTracker) -> None:
    tasks = [await _create(tracker, text=f"task {index}") for index in range(5)]
    await asyncio.gather(
        *(tracker.transition(task.number, "running", run_id=f"run_{task.number}") for task in tasks)
    )

    running = [record["task"] for record in _history(tracker) if record["state"] == "running"]
    assert running == [1, 2, 3, 4, 5]


async def test_started_record_is_on_disk_before_the_announcement(tracker: TaskTracker) -> None:
    seen_in_announcement: list[dict[str, Any]] = []

    async def announce(task: Task) -> None:
        seen_in_announcement.append(_history(tracker)[-1])

    task = await _create(tracker)
    await announce(await tracker.transition(task.number, "running", run_id="run_1"))
    queued = await _create(tracker)
    await announce(await tracker.transition(queued.number, "queued", run_id="run_2"))

    assert [(record["task"], record["state"], record["run_id"]) for record in seen_in_announcement] == [
        (1, "running", "run_1"),
        (2, "queued", "run_2"),
    ]


async def test_per_person_queries(tracker: TaskTracker) -> None:
    rob_old = await _create(tracker, person="rob")
    alex = await _create(tracker, person="alex")
    rob_queued = await _create(tracker, person="rob", profile="stock-picker")
    rob_new = await _create(tracker, person="rob")
    rob_routing = await _create(tracker, person="rob")
    rob_done = await _create(tracker, person="rob")
    await tracker.transition(rob_old.number, "running", run_id="run_1")
    await tracker.transition(alex.number, "running", run_id="run_2")
    await tracker.transition(rob_queued.number, "queued", run_id="run_3")
    await tracker.transition(rob_new.number, "running", run_id="run_4")
    await tracker.transition(rob_done.number, "running", run_id="run_5")
    await tracker.transition(rob_done.number, "completed")

    assert [task.number for task in tracker.active_for("rob")] == [
        rob_old.number,
        rob_queued.number,
        rob_new.number,
    ]
    assert tracker.active_for("nobody") == []
    latest = tracker.latest_running("rob")
    assert latest is not None and latest.number == rob_new.number
    assert tracker.latest_running("nobody") is None
    assert tracker.get(rob_routing.number) == rob_routing
    assert tracker.get(99) is None


async def test_queued_behind_the_task_ahead_on_the_same_person_and_profile(tracker: TaskTracker) -> None:
    first = await _create(tracker, profile="stock-picker")
    other_profile = await _create(tracker, profile="general")
    other_person = await _create(tracker, person="alex", profile="stock-picker")
    second = await _create(tracker, profile="stock-picker")
    third = await _create(tracker, profile="stock-picker")
    await tracker.transition(first.number, "running", run_id="run_1")
    await tracker.transition(other_profile.number, "running", run_id="run_2")
    await tracker.transition(other_person.number, "running", run_id="run_3")
    await tracker.transition(second.number, "queued", run_id="run_4")
    await tracker.transition(third.number, "queued", run_id="run_5")

    assert tracker.ahead_of(first.number) is None
    assert tracker.ahead_of(other_profile.number) is None
    ahead = tracker.ahead_of(second.number)
    assert ahead is not None and ahead.number == first.number
    ahead = tracker.ahead_of(third.number)
    assert ahead is not None and ahead.number == second.number

    await tracker.transition(first.number, "completed")
    assert tracker.ahead_of(second.number) is None


async def test_cancel_stops_the_most_recent_running_task(tracker: TaskTracker) -> None:
    older = await _create(tracker, profile="general")
    newer = await _create(tracker, profile="stock-picker")
    alex = await _create(tracker, person="alex")
    await tracker.transition(older.number, "running", run_id="run_1")
    await tracker.transition(newer.number, "running", run_id="run_2")
    await tracker.transition(alex.number, "running", run_id="run_3")
    stop = _RecordingStop()

    cancelled = await tracker.cancel("rob", stop)

    assert cancelled is not None and cancelled.number == newer.number
    assert cancelled.state == "cancelled"
    assert stop.calls == [("stock-picker", "run_2")]
    assert _history(tracker)[-1]["state"] == "cancelled"
    still_running = tracker.get(older.number)
    assert still_running is not None and still_running.state == "running"


async def test_cancel_by_number(tracker: TaskTracker) -> None:
    older = await _create(tracker, profile="general")
    newer = await _create(tracker, profile="stock-picker")
    await tracker.transition(older.number, "running", run_id="run_1")
    await tracker.transition(newer.number, "running", run_id="run_2")
    stop = _RecordingStop()

    cancelled = await tracker.cancel("rob", stop, number=older.number)

    assert cancelled is not None and cancelled.number == older.number
    assert stop.calls == [("general", "run_1")]


async def test_cancel_ignores_other_peoples_and_finished_tasks(tracker: TaskTracker) -> None:
    alex = await _create(tracker, person="alex")
    done = await _create(tracker)
    await tracker.transition(alex.number, "running", run_id="run_1")
    await tracker.transition(done.number, "running", run_id="run_2")
    await tracker.transition(done.number, "completed")
    stop = _RecordingStop()

    assert await tracker.cancel("rob", stop) is None
    assert await tracker.cancel("rob", stop, number=alex.number) is None
    assert await tracker.cancel("rob", stop, number=done.number) is None
    assert await tracker.cancel("rob", stop, number=99) is None
    assert stop.calls == []


async def test_cancel_leaves_the_task_running_when_stop_fails(tracker: TaskTracker) -> None:
    task = await _create(tracker)
    await tracker.transition(task.number, "running", run_id="run_1")

    with pytest.raises(ConnectionError):
        await tracker.cancel("rob", _RecordingStop(ConnectionError("Hermes is unreachable")))

    still_running = tracker.get(task.number)
    assert still_running is not None and still_running.state == "running"


async def test_cancel_keeps_an_end_reported_while_stop_was_in_flight(tracker: TaskTracker) -> None:
    task = await _create(tracker)
    await tracker.transition(task.number, "running", run_id="run_1")

    async def stop_after_completion(profile: str, run_id: str) -> None:
        await tracker.transition(task.number, "completed")

    result = await tracker.cancel("rob", stop_after_completion)
    assert result is not None and result.state == "completed"


async def test_cancel_without_run_id_is_local_and_logged(
    tracker: TaskTracker, caplog: pytest.LogCaptureFixture
) -> None:
    task = await _create(tracker)
    await tracker.transition(task.number, "queued")
    stop = _RecordingStop()

    with caplog.at_level(logging.WARNING, logger="hermes_duplex.tasks"):
        cancelled = await tracker.cancel("rob", stop, number=task.number)

    assert cancelled is not None and cancelled.state == "cancelled"
    assert stop.calls == []
    assert "may still run it" in caplog.text


async def test_queued_without_run_id_times_out_to_unknown(
    tracker: TaskTracker, caplog: pytest.LogCaptureFixture
) -> None:
    unadmitted = await _create(tracker)
    admitted = await _create(tracker)
    await tracker.transition(unadmitted.number, "queued")
    await tracker.transition(admitted.number, "queued", run_id="run_1")

    with caplog.at_level(logging.WARNING, logger="hermes_duplex.tasks"):
        await asyncio.sleep(0.2)

    expired = tracker.get(unadmitted.number)
    assert expired is not None and expired.state == "unknown"
    still_queued = tracker.get(admitted.number)
    assert still_queued is not None and still_queued.state == "queued"
    assert "task 1 marked unknown" in caplog.text
    assert _history(tracker)[-1]["state"] == "unknown"


async def test_timeout_does_not_touch_a_task_that_left_the_queue(tracker: TaskTracker) -> None:
    task = await _create(tracker)
    await tracker.transition(task.number, "queued")
    await tracker.transition(task.number, "failed")

    await asyncio.sleep(0.2)

    failed = tracker.get(task.number)
    assert failed is not None and failed.state == "failed"


async def test_session_end_expires_its_queued_tasks_without_run_id(tmp_path: Path) -> None:
    tracker = TaskTracker(tmp_path)
    mine = await _create(tracker)
    running = await _create(tracker)
    other_session = await _create(tracker)
    for task in (mine, other_session):
        await tracker.transition(task.number, "queued")
    await tracker.transition(running.number, "running", run_id="run_1")

    await tracker.leave_unattended([mine.number, running.number, 99])

    states = {number: task.state for number in (1, 2, 3) if (task := tracker.get(number))}
    assert states == {mine.number: "unknown", running.number: "running", other_session.number: "queued"}
    await tracker.close()


async def test_unattended_tasks_stay_active_ahead_and_cancellable(tracker: TaskTracker) -> None:
    ended_session = await _create(tracker)
    await tracker.transition(ended_session.number, "running", run_id="run_1")
    await tracker.leave_unattended([ended_session.number])
    new_session = await _create(tracker)
    await tracker.transition(new_session.number, "queued", run_id="run_2")
    stop = _RecordingStop()

    assert [task.number for task in tracker.active_for("rob")] == [ended_session.number, new_session.number]
    ahead = tracker.ahead_of(new_session.number)
    assert ahead is not None and ahead.number == ended_session.number, "Hermes queues behind its run"
    cancelled = await tracker.cancel("rob", stop, number=ended_session.number)
    assert cancelled is not None and cancelled.state == "cancelled"
    assert stop.calls == [("general", "run_1")]


async def test_adopt_hands_out_a_persons_unattended_tasks_once(tracker: TaskTracker) -> None:
    robs = [await _create(tracker), await _create(tracker)]
    alexs = await _create(tracker, person="alex")
    ended = await _create(tracker)
    for number, task in enumerate([*robs, alexs, ended], start=1):
        await tracker.transition(task.number, "running", run_id=f"run_{number}")
    await tracker.transition(ended.number, "completed")
    await tracker.leave_unattended([task.number for task in (*robs, alexs, ended)])

    assert [task.number for task in tracker.adopt("rob").open_tasks] == [task.number for task in robs]
    assert tracker.adopt("rob") == Adoption((), ()), "each task is adopted once"
    assert [task.number for task in tracker.adopt("alex").open_tasks] == [alexs.number]


async def test_a_task_that_ends_while_unattended_is_not_adopted(tracker: TaskTracker) -> None:
    task = await _create(tracker)
    await tracker.transition(task.number, "running", run_id="run_1")
    await tracker.leave_unattended([task.number])
    await tracker.transition(task.number, "cancelled")  # another session cancelled it by number

    assert tracker.adopt("rob") == Adoption((), ())


async def test_untold_ends_are_handed_to_the_persons_next_session_once(tracker: TaskTracker) -> None:
    robs, alexs = await _create(tracker), await _create(tracker, person="alex")
    for task in (robs, alexs):
        await tracker.transition(task.number, "running", run_id=f"run_{task.number}")
        await tracker.transition(task.number, "completed")

    await tracker.leave_unattended([], {robs.number: "While you were away, task 1 finished: done"})

    adoption = tracker.adopt("rob")
    assert [(task.number, text) for task, text in adoption.untold] == [
        (robs.number, "While you were away, task 1 finished: done")
    ]
    assert tracker.adopt("rob").untold == (), "handed out once"
    assert tracker.adopt("alex") == Adoption((), ())


async def test_unattended_after_wakes_when_tasks_are_left_for_adoption(tracker: TaskTracker) -> None:
    task = await _create(tracker)
    await tracker.transition(task.number, "running", run_id="run_1")
    waiter = asyncio.create_task(tracker.unattended_after(0))
    await asyncio.sleep(0.05)
    assert not waiter.done()

    await tracker.leave_unattended([task.number])

    handovers = await asyncio.wait_for(waiter, timeout=1)
    assert handovers == 1
    assert [task.number for task in tracker.adopt("rob").open_tasks] == [task.number]


async def test_restore_brings_back_open_tasks_with_run_ids_as_unattended(tmp_path: Path) -> None:
    previous = TaskTracker(tmp_path)
    running = await _create(previous, text="how's my portfolio")
    await previous.transition(running.number, "running", run_id="run_1")
    queued = await _create(previous, person="alex")
    await previous.transition(queued.number, "queued", run_id="run_2")
    no_run_id = await _create(previous)
    await previous.transition(no_run_id.number, "queued")
    done = await _create(previous)
    await previous.transition(done.number, "running", run_id="run_4")
    await previous.transition(done.number, "completed")
    await previous.close()
    with previous.history_path.open("a", encoding="utf-8") as history:
        history.write("not json\n")

    restored = TaskTracker(tmp_path)
    await restored.restore()

    back = restored.get(running.number)
    assert back is not None
    assert (back.uuid, back.person, back.profile, back.text, back.state, back.run_id, back.created_at) == (
        running.uuid,
        "rob",
        "general",
        "how's my portfolio",
        "running",
        "run_1",
        running.created_at,
    )
    assert [task.number for task in restored.active_for("rob")] == [running.number]
    assert restored.get(no_run_id.number) is None and restored.get(done.number) is None
    assert [task.run_id for task in restored.adopt("alex").open_tasks] == ["run_2"]
    nxt = await _create(restored)
    assert nxt.number == queued.number + 1, "numbers go on after the restored ones"
    await restored.close()


async def test_restore_renumbers_a_task_whose_number_is_taken(tmp_path: Path) -> None:
    for run_id in ("run_a", "run_b"):  # two earlier service runs, each with its own task 1
        earlier = TaskTracker(tmp_path)
        task = await _create(earlier)
        await earlier.transition(task.number, "running", run_id=run_id)
        await earlier.close()

    restored = TaskTracker(tmp_path)
    await restored.restore()

    assert [(task.number, task.run_id) for task in restored.active_for("rob")] == [(1, "run_a"), (2, "run_b")]
    await restored.close()


def _record(task_number: int, run_id: str, created_at: str) -> dict[str, Any]:
    return {
        "task": task_number,
        "uuid": f"uuid-{task_number}",
        "person": "rob",
        "profile": "general",
        "effort": "medium",
        "model": None,
        "text": "hi",
        "run_id": run_id,
        "state": "running",
        "created_at": created_at,
        "updated_at": created_at,
    }


async def test_restore_skips_a_torn_utf8_line_and_reads_naive_timestamps_as_utc(tmp_path: Path) -> None:
    history = tmp_path / "duplex" / "tasks.jsonl"
    history.parent.mkdir(parents=True)
    naive = datetime.now().replace(tzinfo=None).isoformat()  # an older writer's naive local time
    aware = datetime.now(UTC).isoformat()
    with history.open("wb") as file:
        file.write((json.dumps(_record(1, "run_1", naive)) + "\n").encode())
        file.write((json.dumps(_record(2, "run_2", aware)) + "\n").encode())
        file.write('{"task": 3, "text": "caf\u00e9'.encode()[:-1])  # cut inside a two-byte character

    restored = TaskTracker(tmp_path)
    await restored.restore()

    tasks = restored.active_for("rob")
    assert [task.run_id for task in tasks] == ["run_1", "run_2"]
    assert tasks[0].created_at.tzinfo is UTC
    await restored.close()


async def test_restore_marks_tasks_older_than_the_cutoff_unknown_without_restoring_them(
    tmp_path: Path,
) -> None:
    history = tmp_path / "duplex" / "tasks.jsonl"
    history.parent.mkdir(parents=True)
    now = datetime.now(UTC)
    with history.open("w", encoding="utf-8") as file:
        file.write(json.dumps(_record(1, "run_old", (now - timedelta(hours=3)).isoformat())) + "\n")
        file.write(json.dumps(_record(2, "run_new", (now - timedelta(hours=1)).isoformat())) + "\n")

    restored = TaskTracker(tmp_path, restore_max_age_h=2)
    await restored.restore()

    assert [task.run_id for task in restored.active_for("rob")] == ["run_new"]
    assert restored.get(1) is None
    old_lines = [record for record in _history(restored) if record["run_id"] == "run_old"]
    assert [record["state"] for record in old_lines] == ["running", "unknown"]
    await restored.close()

    again = TaskTracker(tmp_path, restore_max_age_h=1000)
    await again.restore()
    assert [task.run_id for task in again.active_for("rob")] == ["run_new"], "it stays out from then on"
    await again.close()


async def test_restore_of_an_unreadable_history_restores_nothing(tmp_path: Path) -> None:
    (tmp_path / "duplex" / "tasks.jsonl").mkdir(parents=True)  # a directory: reading it fails

    tracker = TaskTracker(tmp_path)
    await tracker.restore()

    assert tracker.active_for("rob") == []
    await tracker.close()


async def test_restore_without_a_history_restores_nothing(tracker: TaskTracker) -> None:
    await tracker.restore()

    assert tracker.active_for("rob") == []
    assert (await _create(tracker)).number == 1


async def test_stopping_is_true_only_while_cancel_waits_for_hermes(tracker: TaskTracker) -> None:
    task = await _create(tracker)
    await tracker.transition(task.number, "running", run_id="run_1")
    seen: list[bool] = []

    async def stop(profile: str, run_id: str) -> None:
        seen.append(tracker.stopping(task.number))

    await tracker.cancel("rob", stop)

    assert seen == [True]
    assert not tracker.stopping(task.number)
