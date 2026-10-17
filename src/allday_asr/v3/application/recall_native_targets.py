"""Source-native directory hints and explicit room boundaries; no identity writes."""
from copy import deepcopy

def native_people(people, directory):
    book = deepcopy(people)
    for contact in directory:
        key = "native:" + contact["key"]
        book[key] = {
            "person_id": key,
            "display_name": contact["display_name"],
            "aliases": contact["names"],
            "kind": "contact",
            "platform": contact["platform"],
        }
    return book


def explicit_room(plan, directory):
    mention = plan.get("room_mention")
    if not mention:
        return False
    rooms = [c for c in directory if c["conversation_type"] == "group"
             and mention.casefold() in [n.casefold() for n in c["names"]]]
    if not rooms:
        plan["scope"]["person_id"] = ""
        plan.update(person_candidates=[], contact_candidates=[],
                    issue="当前授权目录未能定位问题指定的群；未改查同昵称的私聊。")
        return True
    # The user named a room and a speaker. A same-label private peer must not
    # replace either boundary. The speaker still needs observed source evidence.
    actor = bool(plan.get("person_mention"))
    plan["scope"].update(person_id="", source_contacts=deepcopy(rooms), relationship="speaker" if actor else "group")
    plan.update(agentic=True, _directory=directory, _contact_scope_is_seed=False,
                person_candidates=[], contact_candidates=[],
                issue="尚待核对指定群内的来源发送者，昵称不等于现实人物绑定。" if actor else "")
    return True
