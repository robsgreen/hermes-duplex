# P1 reproduction (verdict: reviewer-right)

Command (scratch copy): uv run pytest tests/contract/test_realtime_session.py -k "after_the_calling_response" -q -rA → 2 failed (response.create sent while calling response active; receipt 5.4-5.6 ms), other 8 tests pass.
Root cause: realtime.py send_function_output sends response.create immediately; DelegationRequested is mapped on response.function_call_arguments.done (before response.done); session._answer answers at once. Existing fixtures/_responding() send created+call+done in one burst, hiding it.
Keep: function output still within 50 ms; exactly one output per call; _Floor accounting — `_floor.response_requested()` must move with the deferred response.create (or stay matched).

Regression tests to add to tests/contract/test_realtime_session.py (uses existing _Harness, _session, _created, _call, _done, RECEIPT_BUDGET_MS):

```python
async def _response_create_waits_for_the_calling_response(
    harness: _Harness, call_id: str, name: str, arguments: dict[str, Any]
) -> None:
    await harness.server.send(_created(f"resp_{call_id}"), _call(call_id, name, arguments, f"resp_{call_id}"))
    sent_s = time.perf_counter()
    await harness.wait_for(lambda: call_id in harness.outputs(), timeout_s=1)
    receipt_ms = (time.perf_counter() - sent_s) * 1000
    await asyncio.sleep(0.2)
    early = harness.sent("response.create")
    assert receipt_ms < RECEIPT_BUDGET_MS
    assert early == [], "response.create was sent while the calling response was still active"
    await harness.server.send(_done(f"resp_{call_id}"))
    await harness.wait_for(lambda: bool(harness.sent("response.create")), timeout_s=1)
    assert list(harness.outputs()) == [call_id]


async def test_delegate_output_asks_for_a_response_only_after_the_calling_response_is_done(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    async with _session(tmp_path, api) as harness:
        await _response_create_waits_for_the_calling_response(
            harness, "call_1", "delegate", {"task": "what's the weather"}
        )


async def test_task_status_output_asks_for_a_response_only_after_the_calling_response_is_done(
    tmp_path: Path, api: FakeHermesApi
) -> None:
    async with _session(tmp_path, api) as harness:
        await _response_create_waits_for_the_calling_response(harness, "call_2", "task_status", {})
```
