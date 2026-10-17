"""Native metadata refresh preserves scoped aliases, never merges people."""
import json
import time
from threading import Event
from types import SimpleNamespace

from allday_asr.v3.adapters.sqlite.recall_directory import RecallDirectory, entry, merge_entries


def item(name="Current", account="source-a", peer="peer-a", platform="wechat"):
    return entry(dict(platform=platform, source_account_id=account,
                      id="room", conversation_type="c2c", display_name=name,
                      peer_account_id=peer), "dataset-a")


def test_refresh_preserves_scoped_aliases_and_current_canonical_label(tmp_path):
    old = item("Observed alias")
    path = tmp_path / "chat/recall-directory.json"
    path.parent.mkdir()
    path.write_text(json.dumps(dict(dataset="dataset-a", items=[old], pages={})))
    ids = SimpleNamespace(chat_path=tmp_path / "absent-cache")

    class Source:
        def request(self, route, **kwargs):
            if route == "/v1/status":
                return {"dataset_id": "dataset-a"}
            return dict(items=[dict(platform="wechat", source_account_id="source-a",
                        id="room", conversation_type="c2c", display_name="Current",
                        peer_account_id="peer-a")], has_more=False)

    directory = RecallDirectory(ids, Source(), tmp_path)
    rows, _, requests = directory.load("wechat", True, Event(), time.monotonic() + 2)
    assert requests == 2 and len(rows) == 1
    assert rows[0]["display_name"] == "Current"
    assert rows[0]["names"] == ["Current", "Observed alias"]
    # New adapter/process and expired live pages must retain the same effective index.
    state = json.loads(path.read_text())
    state["pages"] = {}
    path.write_text(json.dumps(state))
    restarted = RecallDirectory(ids, Source(), tmp_path)
    again, _, _ = restarted.load("wechat", True, Event(), time.monotonic() + 2)
    assert again == rows
    assert merge_entries(rows, [item("Observed alias")]) == rows


def test_aliases_never_cross_namespace_or_changed_native_peer():
    old = item("Old")
    fresh = item("Current", peer="other-peer")
    merged = merge_entries([old], [fresh], current=True)
    assert merged[0]["names"] == ["Current"]
    assert merged[0]["sender_account_id"] == "other-peer"
    assert merge_entries([old], [fresh])[0]["names"] == ["Old"]
    for other in [item("Current", account="source-b"), item("Current", platform="qq")]:
        result = merge_entries([old], [other], current=True)
        assert len(result) == 2
        assert result[1]["names"] == ["Current"]


def test_alias_order_is_stable_under_cached_page_permutation():
    base = item("Current")
    aliases = [item("Zulu"), item("Alpha")]
    assert merge_entries([base], aliases) == merge_entries([base], aliases[::-1])
    assert merge_entries([base], aliases)[0]["display_name"] == "Current"


def test_current_native_fields_replace_cached_fields_without_losing_aliases():
    old = item("Old")
    fresh = {**item("Current"), "conversation_type": "group"}
    merged = merge_entries([old], [fresh], current=True)[0]
    assert merged["conversation_type"] == "group"
    assert set(merged["names"]) == {"Old", "Current"}
