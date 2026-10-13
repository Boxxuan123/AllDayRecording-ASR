"""Conservative discussion objects; budgets and citation pages do not define boundaries."""
import re
from allday_asr.v3.domain.chat_projection import semantic_text
from allday_asr.v3.domain.memory_content import readable

RULE_VERSION = 2

def object_key(text):
    if re.search(r"打光|亮度|手电筒|相机拍|黑衬衣", text): return "lighting"
    if re.search(r"报哪个专业|计算机技术|人工智能|报考|专业选择|院校选择|报名专业", text): return "application"
    if re.search(r"政治.*小册|刷题|复习资料", text): return "study_material"
    if text.startswith(("http://","https://")): return "navigation_context"
    if re.search(r"看看照片|选哪一张|这张好看|报名照片|拍照|背景.*[批抠]", text): return "photo"
    if re.search(r"不让登|绑定微信|浏览器|微信解绑|复制链接|才能登录|不登录", text): return "access"
    unique = re.search(r"(?:独立机会编号|具体事情编号)\d+|第[一二三四五六七八九十]+次招聘机会|另一次招聘机会", text)
    if unique: return unique.group()
    if re.search(r"签到|积分|连续签到|点赞",text): return "routine_rewards"
    if re.search(r"国庆.*购机|购机.*活动|到店.*优惠|询价|多少钱|想买",text): return "purchase"
    if re.search(r"物理.*招聘|招聘.*物理|尚未.*回复|婉拒|初中物理|高中教学", text): return "recruitment"
    return None

def split(records):
    groups = []
    current = None
    for r in records:
        text = semantic_text(r.get("text") or "")
        key = object_key(text)
        # Access troubleshooting can continue a photo-selection request only when
        # the preceding originals actually offer a link and ask to choose a photo.
        bridge = bool(current and current["object"] == "photo" and key == "access"
                      and any(semantic_text(x.get("text") or "").startswith("http") for x in current["records"]))
        if key and current and current["object"] is not None and key != current["object"] and not bridge and not (current["object"]=="navigation_context" and key=="photo"):
            current = None
        if current and current["object"] is None and key is None and readable(text) and len(text.strip())>=6:
            prior=[semantic_text(x.get("text") or "") for x in current["records"] if readable(semantic_text(x.get("text") or ""))]
            explicit_reply=bool((r.get("reply") or {}).get("record_id")) if isinstance(r.get("reply"),dict) else False
            # With no identifiable object or reply edge, a substantive statement
            # stays independently uncertain. Short responses remain nearby context.
            if prior and not explicit_reply: current=None
        if current is None:
            current = {"records": [], "object": key, "boundary_basis": "explicit_discussion_object" if key else "uncertain_independent_context"}
            groups.append(current)
        elif (current["object"] is None or current["object"]=="navigation_context" and key=="photo") and key:
            current["object"] = key
            current["boundary_basis"] = "explicit_discussion_object"
        # A new self-contained numbered business object always starts independently.
        current["records"].append(r)
    return groups

def role(record):
    text = semantic_text(record.get("text") or "")
    return "core" if readable(text) and not text.startswith("http") else "context"
