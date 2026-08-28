from __future__ import annotations

import unittest

from allday_asr.services.review import parse_review_markdown


class ReviewTests(unittest.TestCase):
    def test_parse_review_markdown_maps_labels_and_revision(self) -> None:
        text = """# review

## 1. segment 230 · 通过严格阈值（我）
- 文字：测试

## 2. segment 240 · 仅供比较（我+我妈）
- 修订：妈妈一句，我一句

## 3. segment 164 · 仅供比较（太短，听不出来）
"""
        annotations = parse_review_markdown(text)
        self.assertEqual(
            [item["identity_label"] for item in annotations],
            ["self", "mixed", "uncertain"],
        )
        self.assertEqual(annotations[1]["note"], "妈妈一句，我一句")
        self.assertEqual(annotations[2]["confidence"], "uncertain")


if __name__ == "__main__":
    unittest.main()
