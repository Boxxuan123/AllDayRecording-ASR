"""Structural contracts, independent of speaker accuracy or private recordings."""
import pytest

from allday_asr.v3.adapters.models.native_projection import _group_utterances
from allday_asr.v3.domain.blind_windows import plan_source_windows
from allday_asr.v3.domain.speaker_turns import PROJECTION_VERSION, foreign_turns


def token(start, end, speaker="A", text="sample"):
    return dict(start_ms=start, end_ms=end, speaker=speaker, text=text, source_refs=[])


def turn(start, end, speaker="A"):
    return dict(start_ms=start, end_ms=end, speaker_label=speaker)


def capture(start=0, end=30000, media="one", session="session"):
    return dict(session_id=session, session_start_ms=start, session_end_ms=end,
                source_start_ms=0, source_end_ms=end-start, duration_ms=end-start,
                media_id=media, sha256=media, storage_key=media)


def plan(end, turns=(), tokens=(), vad=(), captures=None):
    return plan_source_windows(dict(start_ms=0, end_ms=end, utterance_id="u"),
        "A", "identity-track", turns, tokens, vad, captures or [capture()], PROJECTION_VERSION)


@pytest.mark.parametrize("duration", [300, 500, 1000])
@pytest.mark.parametrize("b_has_text", [True, False])
def test_aba_never_fills_foreign_turn(duration, b_has_text):
    turns = [turn(0, 2000), turn(2000, 2000+duration, "B"), turn(2000+duration, 4000+duration)]
    tokens = [token(0,2000), token(2000+duration,4000+duration)]
    if b_has_text:
        tokens.insert(1, token(2000,2000+duration,"B"))
    groups = _group_utterances(tokens, turns)
    assert [g["speaker"] for g in groups] == (["A", "B", "A"] if b_has_text else ["A", "A"])
    assert sum(g["token_count"] for g in groups) == len(tokens)
    assert all(not foreign_turns(g["start_ms"], g["end_ms"], g["speaker"], turns) for g in groups)


@pytest.mark.parametrize("gap,merged", [(0,True),(1200,True),(1201,False)])
def test_silence_threshold_unchanged(gap, merged):
    assert len(_group_utterances([token(0,2000),token(2000+gap,4000+gap)])) == (1 if merged else 2)


def test_crossing_token_is_not_faked_or_lost_and_regular_overlap_not_barrier():
    tokens = [token(0,2500,text="crossing"), token(2500,4000,text="later")]
    groups = _group_utterances(tokens, [turn(0,2000),turn(2000,2300,"B"),turn(2300,4000)])
    assert groups[0]["speaker"] is None and groups[0]["text"] == "crossing"
    assert groups[0]["token_attributions"][0]["attributed_speaker"] == "A"
    assert groups[0]["speaker_boundary_crossing"]
    assert sum(g["token_count"] for g in groups) == 2
    # Regular simultaneous overlap is separate evidence, not supplied as exclusive turns.
    assert len(_group_utterances(tokens, [turn(0,4000)])) == 1
    assert len(_group_utterances([token(0,16000), token(16000,31000)])) == 2


def test_complete_short_and_long_turns_bounded_with_explicit_hard_caps():
    assert [(w["session_start_ms"],w["session_end_ms"]) for w in plan(7000)["windows"]] == [(0,7000)]
    result = plan(17000, [turn(0,17000)])
    assert [w["end_ms"]-w["start_ms"] for w in result["windows"]] == [8000,8000,1000]
    assert [w["provenance"]["end_boundary_reason"] for w in result["windows"]] == ["hard_cap","hard_cap","speaker_turn"]


@pytest.mark.parametrize("turns,tokens,vad,reason,stop", [
    ([turn(0,7400),turn(7400,17000)], [], [], "speaker_turn",7400),
    ([], [token(0,7350),token(7350,9000)], [], "token",7350),
    ([], [], [(0,7600)], "vad",7600),
    ([], [token(0,8100)], [(0,7500)], "hard_cap",8000)])
def test_natural_boundary_preference_and_no_token_middle(turns,tokens,vad,reason,stop):
    window = plan(17000,turns,tokens,vad)["windows"][0]
    assert window["end_ms"] == stop
    assert window["provenance"]["end_boundary_reason"] == reason


def test_subtract_foreign_and_reliable_capture_continuation():
    result = plan(17000,[turn(0,7000),turn(7000,7300,"B"),turn(7300,17000)],
                  captures=[capture(0,10000),capture(10000,20000,"two")])
    assert any(w["media_id"] == "two" for w in result["windows"])
    assert any(w["provenance"]["end_boundary_reason"] == "capture_edge_continue" for w in result["windows"])
    assert all(not foreign_turns(w["session_start_ms"],w["session_end_ms"],"A",[turn(7000,7300,"B")]) for w in result["windows"])
    assert result == plan(17000,[turn(0,7000),turn(7000,7300,"B"),turn(7300,17000)],
                         captures=[capture(0,10000),capture(10000,20000,"two")])


@pytest.mark.parametrize("next_capture", [None,capture(10001,20000,"two"),
                                         capture(10000,20000,"two") | {"duration_ms": 1}])
def test_storage_tail_never_silently_disappears(next_capture):
    result = plan(17000,captures=[capture(0,10000)] + ([next_capture] if next_capture else []))
    assert any(e["start_ms"] == 10000 and e["end_ms"] == 17000 for e in result["exclusions"])
    assert result["windows"][-1]["provenance"]["end_boundary_reason"] == "media_end"
    with pytest.raises(ValueError,match="recording sessions"):
        plan(17000,captures=[capture(0,10000),capture(10000,20000,"two","other")])
