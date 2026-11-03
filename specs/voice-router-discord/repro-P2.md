# P2 reproduction (verdict: reviewer-right; tester mislabelled as "reproduced-correct" but evidence shows the bug)

Command: from the worktree, a scratch test importing tests/unit/test_receiver.py fixtures → 2 failed (stranger delivered as 'rob', DAVE off and on), 3 passed (preserve cases).
Existing test to update: tests/unit/test_receiver.py::test_unmapped_ssrc_is_inferred_from_the_sole_allowlisted_member (pins the bug: members [ROB_ID, STRANGER_ID]).

Regression tests to add to tests/unit/test_receiver.py (fixtures already there):

```python
@pytest.mark.parametrize("dave_on", [False, True])
async def test_stranger_on_unmapped_ssrc_is_dropped_not_credited_to_the_allowlisted_member(
    start: Any, dave_on: bool
) -> None:
    session = StubDaveSession() if dave_on else None
    harness = start([ROB_ID, STRANGER_ID], session)
    harness.send(rtp(ALEX_SSRC, b"stranger-opus"))
    assert await harness.delivered() == []
    assert FakeDecoder.decoded == []


@pytest.mark.parametrize("dave_on", [False, True])
async def test_unmapped_ssrc_is_inferred_when_the_channel_holds_only_the_bot_and_one_allowlisted_person(
    start: Any, dave_on: bool
) -> None:
    session = StubDaveSession() if dave_on else None
    harness = start([ROB_ID], session)
    payload = dave(ROB_ID, b"rob-opus") if dave_on else b"rob-opus"
    harness.send(rtp(ROB_SSRC, payload))
    assert [person for person, _ in await harness.delivered()] == ["rob"]
    assert FakeDecoder.decoded == [b"rob-opus"]


async def test_speaking_remaps_an_inferred_ssrc_to_the_stranger_who_is_then_dropped(start: Any) -> None:
    harness = start([ROB_ID])
    harness.send(rtp(ALEX_SSRC, b"inferred", seq=1))
    await harness.speaking(ALEX_SSRC, STRANGER_ID)
    harness.send(rtp(ALEX_SSRC, b"after-remap", seq=2))
    assert [person for person, _ in await harness.delivered()] == ["rob"]
    assert FakeDecoder.decoded == [b"inferred"]
```
Note: with P3's fix (drop "Unencrypted" when passthrough disabled), the DAVE-on stranger case is dropped by both fixes; StubDaveSession should model davey passthrough (enabled → return packet, disabled → raise).
