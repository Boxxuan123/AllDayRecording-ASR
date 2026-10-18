"""Local semantic views of cached chat; raw source storage is never rewritten."""

import html
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urlunsplit

_ACCESS = re.compile(
    r"aeskey|encryptkey|authkey|access.?token|password|cdn.*(?:url|key)|media(?:url|uri)|thumburl|fileurl|filekey|attachurl|signature|^token$|^url$|^uri$|local.?path|file.?path|^attachments$|source_locator",
    re.I,
)
_XML = re.compile(
    r"<(?:msg|appmsg|img|video|videomsg|refermsg|recorditem|item|appinfo|weappinfo|root)\b",
    re.I,
)
_SEMANTIC = {
    "title",
    "des",
    "content",
    "text",
    "displayname",
    "fromusr",
    "chatusr",
    "fromusername",
    "createtime",
    "name",
    "topic",
    "srcname",
    "sourcename",
    "sourcetime",
    "datadesc",
    "datatitle",
}
_MEDIA = re.compile(r"https?://\S*(?:cdn|qpic|qlogo|mmwx|wxapp)\S*", re.I)


def transport_xml(value):
    """Relocate one valid document prolog for a text+XML transport envelope.

    Only the first markup may be a prolog; malformed/repeated declarations and
    DTDs remain failures. Prefix text is content, never sender identity.
    """
    first = value.find("<")
    if first < 0 or not value.startswith("<?xml ", first):
        return value
    end = value.find("?>", first)
    if end < 0:
        return value
    declaration = value[first:end + 2]
    try:
        ET.fromstring(declaration + "<projection/>")
    except (ET.ParseError, ValueError):
        return value
    return value[:first] + value[end + 2:]


def semantic_text(value, depth=0):
    if not isinstance(value, str):
        return value
    if depth > 8 or len(value) > 1_000_000:
        return "[未解析：内容超出本地投影限制]"
    decoded = value
    for _ in range(3):
        next_value = html.unescape(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    if "<!DOCTYPE" in decoded.upper() or "<!ENTITY" in decoded.upper():
        return "[未解析：禁止外部实体或文档声明]"
    image = bool(re.search(r"<img\b", decoded, re.I))
    if _XML.search(decoded):
        try:
            root = ET.fromstring(decoded)
        except (ET.ParseError, ValueError):
            try:
                root = ET.fromstring("<projection>" + transport_xml(decoded) + "</projection>")
            except (ET.ParseError, ValueError):
                prefix = decoded.split("<", 1)[0].strip()
                if image:
                    return (semantic_text(prefix, depth + 1) + "\n" if prefix else "") + "收到图片，内容未解析"
                return (
                    semantic_text(prefix, depth + 1) + "\n" if prefix else ""
                ) + "[未解析：媒体/引用 XML；原文仅保留在来源缓存]"
        parts = []

        def visit(node, level):
            if level > 32:
                parts.append("[未解析：嵌套层级超限]")
                return
            tag = node.tag.rsplit("}", 1)[-1].lower()
            if _ACCESS.search(tag):
                return
            if tag == "refermsg":
                parts.append("[引用开始]")
            if tag in _SEMANTIC and node.text and node.text.strip():
                text = semantic_text(node.text.strip(), depth + 1)
                if text:
                    parts.append(tag + ": " + text)
            if (
                tag in {"projection", "msg", "appmsg"}
                and node.text
                and node.text.strip()
                and not _XML.search(node.text)
            ):
                parts.append(semantic_text(node.text.strip(), depth + 1))
            if (
                tag not in _SEMANTIC
                and node.text
                and _XML.search(html.unescape(node.text))
            ):
                parts.append(
                    "[转发/嵌套开始]"
                    + semantic_text(node.text.strip(), depth + 1)
                    + "[转发/嵌套结束]"
                )
            for child in node:
                visit(child, level + 1)
                if child.tail and child.tail.strip():
                    parts.append(semantic_text(child.tail.strip(), depth + 1))
            if tag == "refermsg":
                parts.append("[引用结束]")

        visit(root, 0)
        return "\n".join(parts) or ("收到图片，内容未解析" if image else "[媒体消息：附件访问材料已省略]")
    # Also cover attachment attributes in otherwise non-XML fragments; never emit values.
    if re.search(r"(?:aeskey|cdnthumbaeskey|access.?token|authkey|encryptkey|password|signature)\s*[=：:]", decoded, re.I):
        return "[未解析：包含附件访问材料的片段]"
    def public_url(match):
        try:
            u = urlsplit(match.group(0))
            return urlunsplit((u.scheme, u.hostname or "", u.path, "", ""))
        except ValueError:
            return "[链接访问参数已省略]"
    return re.sub(r"https?://[^\s<>\"']+", public_url, _MEDIA.sub("[媒体定位已省略]", decoded))


def semantic_value(value):
    if isinstance(value, str):
        return semantic_text(value)
    if isinstance(value, list):
        return [semantic_value(v) for v in value]
    if isinstance(value, dict):
        return {k: semantic_value(v) for k, v in value.items() if not _ACCESS.search(k)}
    return value


def semantic_record(record):
    # An allowlist prevents raw attachment/locator extensions escaping through exports.
    fields = (
        "platform",
        "source_account_id",
        "conversation_id",
        "conversation_type",
        "record_id",
        "record_revision",
        "is_from_self",
        "self_identity_basis",
        "sender_account_id",
        "sender_display_name",
        "sender_name",
        "sent_at",
        "text",
        "reply",
        "quote",
        "forward",
        "deleted",
    )
    return semantic_value({k: record[k] for k in fields if k in record})
