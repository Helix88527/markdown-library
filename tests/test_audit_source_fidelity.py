"""原材料覆盖清单与成稿落点测试。"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_source_fidelity.py"
SPEC = importlib.util.spec_from_file_location("audit_source_fidelity", SCRIPT)
assert SPEC and SPEC.loader
auditor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auditor)


class SourceFidelityAuditTests(unittest.TestCase):
    def prepare_fixture(self, root: Path) -> tuple[Path, Path, Path, dict]:
        source = root / "02_校正逐字稿.md"
        final = root / "节目.md"
        report = root / "内容覆盖清单.json"
        source.write_text(
            "[00:00:01] 主持人说，甲公司在2024年投入3亿元。为什么会这样？\n\n"
            "[00:05:00] 嘉宾回答，原因有两点，并举了乙项目作为例子。",
            encoding="utf-8",
        )
        final.write_text(
            "节目称，甲公司在2024年投入3亿元；这仍是节目主张。"
            "对于‘为什么会这样’，嘉宾给出两点原因，并以乙项目为例。",
            encoding="utf-8",
        )
        payload = auditor.prepare_report(source, report, max_chars=400)
        unit = payload["units"][0]
        unit["information_units"] = [
            {
                "kind": "claim",
                "source_detail": "主持人说，甲公司在2024年投入3亿元",
                "final_evidence": "节目称，甲公司在2024年投入3亿元",
            },
            {
                "kind": "example",
                "source_detail": "嘉宾回答，原因有两点，并举了乙项目作为例子",
                "final_evidence": "嘉宾给出两点原因，并以乙项目为例",
            },
        ]
        for anchor in unit["numeric_anchors"]:
            anchor["status"] = "covered"
            anchor["final_evidence"] = "节目称，甲公司在2024年投入3亿元"
        for anchor in unit["question_anchors"]:
            anchor["status"] = "covered"
            anchor["final_evidence"] = "对于‘为什么会这样’，嘉宾给出两点原因"
        report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return source, final, report, payload

    def test_complete_detailed_ledger_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, final, report, _ = self.prepare_fixture(Path(temporary))
            result = auditor.audit_report(source, final, report)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["unit_count"], 1)
            self.assertEqual(result["information_unit_count"], 2)
            saved = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(saved["final"]["sha256"], result["final_sha256"])

    def test_vague_summary_cannot_pass_as_information_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, final, report, payload = self.prepare_fixture(Path(temporary))
            payload["units"][0]["information_units"][0]["source_detail"] = "本段内容已覆盖"
            report.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(auditor.FidelityError, "过于笼统"):
                auditor.audit_report(source, final, report)

    def test_final_evidence_must_exist_in_final_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, final, report, payload = self.prepare_fixture(Path(temporary))
            payload["units"][0]["information_units"][0]["final_evidence"] = "成稿里根本没有这句话"
            report.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(auditor.FidelityError, "不是最终 Markdown"):
                auditor.audit_report(source, final, report)

    def test_source_detail_must_be_a_continuous_source_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, final, report, payload = self.prepare_fixture(Path(temporary))
            payload["units"][0]["information_units"][0]["source_detail"] = "原材料里没有这项说法"
            report.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(auditor.FidelityError, "不是当前来源块"):
                auditor.audit_report(source, final, report)

    def test_duplicate_final_evidence_cannot_cover_different_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, final, report, payload = self.prepare_fixture(Path(temporary))
            payload["units"][0]["information_units"][1]["final_evidence"] = payload["units"][0][
                "information_units"
            ][0]["final_evidence"]
            report.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(auditor.FidelityError, "重复占位"):
                auditor.audit_report(source, final, report)

    def test_long_source_unit_cannot_pass_with_one_token_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "02_校正逐字稿.md"
            final = root / "节目.md"
            report = root / "内容覆盖清单.json"
            source.write_text("主持人提出第一项具体主张。" + "嘉宾继续补充不同背景与理由。" * 45, encoding="utf-8")
            final.write_text("成稿保留主持人提出的第一项具体主张。", encoding="utf-8")
            payload = auditor.prepare_report(source, report)
            self.assertGreater(payload["units"][0]["minimum_information_units"], 1)
            payload["units"][0]["information_units"] = [
                {
                    "kind": "claim",
                    "source_detail": "主持人提出第一项具体主张",
                    "final_evidence": "成稿保留主持人提出的第一项具体主张",
                }
            ]
            report.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(auditor.FidelityError, "低于机械下限"):
                auditor.audit_report(source, final, report)

    def test_pending_number_or_question_blocks_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, final, report, payload = self.prepare_fixture(Path(temporary))
            payload["units"][0]["numeric_anchors"][0]["status"] = "pending"
            report.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(auditor.FidelityError, "pending"):
                auditor.audit_report(source, final, report)

    def test_source_change_invalidates_prepared_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, final, report, _ = self.prepare_fixture(Path(temporary))
            source.write_text(source.read_text(encoding="utf-8") + "\n新增一句。", encoding="utf-8")
            with self.assertRaisesRegex(auditor.FidelityError, "SHA256 已变化"):
                auditor.audit_report(source, final, report)


if __name__ == "__main__":
    unittest.main()
