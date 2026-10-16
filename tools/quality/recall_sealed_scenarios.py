"""Independent synthetic generator. Write sealed gold separately; never read it in product runs."""

import argparse
import hashlib
import json
import secrets
import uuid
from pathlib import Path
from tests.recall_scenario_driver import contact, message, DAY


def generate(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    runtime = destination / "runtime"
    gold = destination / "sealed-gold"
    runtime.mkdir()
    gold.mkdir()
    names = [
        "许沅",
        "沈遥",
        "陶岚",
        "顾澄",
        "林朔",
        "钟宁",
        "江岑",
        "陆榆",
        "程栩",
        "贺楠",
    ]
    topics = [
        (
            "场地",
            "昨晚{n}准备在哪儿碰面",
            "今晚在哪碰头？",
            ["旧钟楼西门", "杉树林入口", "东侧长廊"],
            "{v}，先这样打算，还没有出发。",
            "计划，未证实实际到达",
        ),
        (
            "交通",
            "昨天晚上{n}准备怎么回去",
            "今晚回去坐什么？",
            ["末班公交", "同事的顺风车", "校内摆渡车"],
            "想坐{v}，如果停运就再说。",
            "有条件的计划",
        ),
        (
            "设备",
            "昨天{n}为什么没有打开投影",
            "投影为什么没开？",
            ["转接头没带", "幕布电源坏了", "预约还没确认"],
            "因为{v}，先取消展示。",
            "否定与原因",
        ),
        (
            "材料",
            "昨晚{n}的材料送到了吗",
            "材料真的到了？",
            ["值班同学", "临时领队", "另一组的助理"],
            "只是听{v}说送到了，我还没核实。",
            "第三人转述，实际未知",
        ),
        (
            "面谈",
            "昨天{n}约了几点见面",
            "见面的时间定了吗？",
            ["十九点二十", "二十点十分", "十八点四十"],
            "先按{v}，等对方确认。",
            "待确认的约定",
        ),
        (
            "借阅",
            "昨天{n}最后借到书了吗",
            "书借到了没？",
            ["管理员临时外出", "预约被取消", "系统维护"],
            "没借到，因为{v}；明天再议。",
            "未发生与后续打算",
        ),
        (
            "服装",
            "昨天晚上{n}想穿什么",
            "晚上那场你穿啥？",
            ["深色外套", "浅色衬衫", "旧运动衫"],
            "{v}吧，还没换上。",
            "口语省略的计划",
        ),
        (
            "排班",
            "昨晚{n}最终值班安排是什么",
            "你最后怎么排班？",
            ["只守前半场", "由同组代班", "推迟到次日"],
            "原来要通宵，现在改成{v}；还没开始。",
            "更正而非完成",
        ),
    ]
    manifest = []
    for index, (theme, q, ask, values, reply, kind) in enumerate(topics):
        name = secrets.choice(names)
        value = secrets.choice(values)
        peer = uuid.uuid4().hex
        room = uuid.uuid4().hex
        direct = uuid.uuid4().hex
        other = uuid.uuid4().hex
        token = uuid.uuid4().hex[:12]
        contacts = [
            contact("wechat", uuid.uuid4().hex, name, uuid.uuid4().hex),
            contact(
                "qq", direct, secrets.choice(["同学", "同事", "项目组"]) + name, peer
            ),
            contact("qq", room, "临时讨论 " + uuid.uuid4().hex[:4], kind="group"),
        ]
        rows = [
            message(
                index * 1000 + 1,
                "这是去年的旧资料。",
                contacts[0]["peer_account_id"],
                contacts[0]["id"],
                "wechat",
                kind="c2c",
                day=DAY - 120 * 86400,
                label=name,
            ),
            message(
                index * 1000 + 2,
                "收到了，稍后再说。",
                peer,
                direct,
                kind="c2c",
                label="原生昵称" + uuid.uuid4().hex[:4],
            ),
            message(index * 1000 + 3, ask, other, room, minute=2, label="同组成员"),
            message(
                index * 1000 + 4,
                reply.format(v=value),
                peer,
                room,
                minute=3,
                label="原生昵称" + uuid.uuid4().hex[:4],
            ),
        ]
        if index % 3 == 1:
            rows.append(
                message(
                    index * 1000 + 5, "好，等你的确认。", "self-account", room, minute=4
                )
            )
        # Rename all native identities, not just displayed people. Gold derives solely from generated originals.
        for row in rows:
            row["record_id"] = "rec:" + uuid.uuid4().hex
            row["source_locator"]["native_message_id"] = uuid.uuid4().hex
        topology = "cross_platform_group"
        filters = {}
        expected_unknown = False
        if index == 1:
            topology = "private_question_answer"
            contacts = contacts[1:2]
            for row in rows[2:]:
                row.update(conversation_id=direct, conversation_type="c2c")
                row["source_locator"]["conversation_id"] = direct
            rows = rows[1:]
        elif index == 2:
            topology = "single_wechat_group"
            contacts = contacts[1:]
            for c in contacts:
                c.update(platform="wechat", source_account_id="source-wechat")
            rows = rows[1:]
            for row in rows:
                row.update(platform="wechat", source_account_id="source-wechat")
                row["source_locator"].update(
                    platform="wechat", source_account_id="source-wechat"
                )
        elif index == 3:
            topology = "missing_peer_historical_bridge"
            contacts[1]["peer_account_id"] = None
            rows[1]["sent_at"] = DAY - 12 * 86400
        elif index == 4:
            topology = "topic_observed_sender_bridge"
            topic = "研讨" + uuid.uuid4().hex[:8]
            contacts = contacts[1:]
            contacts[0].update(
                conversation_type="group", display_name=topic, peer_account_id=None
            )
            rows = rows[1:]
            rows[0].update(
                conversation_type="group",
                text="请查收研讨通知，稍后讨论。",
                sent_at=DAY - 3 * 86400,
            )
            q = "昨天晚上" + topic + "发生了什么"
            name = ""
        elif index == 5:
            topology = "same_name_separate_owner_namespaces"
            from copy import deepcopy

            competitor = deepcopy(contacts[1])
            competitor.update(
                source_account_id="other-qq-owner",
                id=uuid.uuid4().hex,
                peer_account_id=uuid.uuid4().hex,
            )
            contacts.append(competitor)
            extra = message(
                999999,
                "只听别人说办完了；本人没有确认。",
                competitor["peer_account_id"],
                competitor["id"],
                kind="c2c",
                label=name,
            )
            extra.update(
                record_id="rec:" + uuid.uuid4().hex,
                source_account_id=competitor["source_account_id"],
            )
            extra["source_locator"].update(
                source_account_id=competitor["source_account_id"]
            )
            rows.append(extra)
        elif index == 6:
            topology = "explicit_platform_with_other_platform_decoy"
            filters = {"source": "qq"}
        elif index == 7:
            topology = "question_without_answer"
            rows = rows[:3]
            expected_unknown = True
        required = [
            r["record_id"]
            for r in rows
            if r.get("text") == ask or r.get("text") == reply.format(v=value)
        ]
        secrets.SystemRandom().shuffle(contacts)
        scene = dict(
            question=q.format(n=name), contacts=contacts, records=rows, filters=filters
        )
        answer = dict(
            required_record_ids=required,
            topology=topology,
            expected_unknown=expected_unknown,
            source_kind=kind,
            expected_phrase="" if expected_unknown else value,
            event_date="2026-10-15",
            message_date="2026-10-15",
            forbidden_fact_upgrade=True,
            theme=theme,
        )
        case = runtime / (token + ".json")
        case.write_text(json.dumps(scene, ensure_ascii=False, indent=2), "utf8")
        seal = gold / (token + ".json")
        seal.write_text(json.dumps(answer, ensure_ascii=False, indent=2), "utf8")
        manifest.append(
            dict(
                case=case.name,
                runtime_sha256=hashlib.sha256(case.read_bytes()).hexdigest(),
                sealed_gold_sha256=hashlib.sha256(seal.read_bytes()).hexdigest(),
            )
        )
    (destination / "sealed-manifest.json").write_text(
        json.dumps(manifest, indent=2), "utf8"
    )
    print("SEALED", len(manifest), "independent scenes; gold NOT displayed")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("destination")
    generate(p.parse_args().destination)
