"""输出位置规划与入库边界测试。"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plan_output.py"
SPEC = importlib.util.spec_from_file_location("plan_output", SCRIPT)
assert SPEC and SPEC.loader
planner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(planner)


class OutputPlanTests(unittest.TestCase):
    """保证入库依据当前任务要求，同时保持项目目录不变。"""

    def test_external_file_without_database_keeps_location_and_reports_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "下载" / "节目.mp4"
            source.parent.mkdir()
            source.write_bytes(b"media")

            result = planner.build_output_plan(source)

            self.assertEqual(Path(result["project_dir"]), source.parent.resolve())
            self.assertEqual(result["location_basis"], "source_parent")
            self.assertFalse(result["writes_performed"])
            self.assertFalse(result["ingest_allowed"])
            self.assertFalse(result["standing_ingest_authorized"])
            self.assertFalse(result["automatic_backup_required"])
            self.assertFalse(result["automatic_backup_ready"])
            self.assertEqual(result["ingest_authorization_basis"], "not_requested")

    def test_raw_library_source_does_not_imply_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / planner.ROOT_NAME
            raw = root / planner.RAW_LIBRARY_DIRNAME / "视频资料" / "节目"
            raw.mkdir(parents=True)
            (root / planner.DATABASE_MARKER).write_text("{}", encoding="utf-8")
            source = raw / "节目.mp4"
            source.write_bytes(b"media")

            result = planner.build_output_plan(source)

            self.assertTrue(result["source_inside_raw_library"])
            self.assertFalse(result["standing_ingest_authorized"])
            self.assertFalse(result["ingest_allowed"])
            self.assertFalse(result["automatic_backup_required"])
            self.assertTrue(result["automatic_backup_ready"])
            self.assertFalse(result["ingest_confirmed"])
            self.assertEqual(
                result["ingest_authorization_basis"],
                "not_requested",
            )

    def test_database_non_raw_source_does_not_imply_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / planner.ROOT_NAME
            source = root / "其他资料" / "节目.mp4"
            source.parent.mkdir(parents=True)
            (root / planner.DATABASE_MARKER).write_text("{}", encoding="utf-8")
            source.write_bytes(b"media")

            result = planner.build_output_plan(source)

            self.assertTrue(result["source_inside_database"])
            self.assertFalse(result["source_inside_raw_library"])
            self.assertFalse(result["ingest_allowed"])
            self.assertFalse(result["standing_ingest_authorized"])

    def test_explicit_output_is_preserved_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "来源.txt"
            destination = root / "用户指定成果"
            source.write_text("正文", encoding="utf-8")

            result = planner.build_output_plan(source, output_dir=destination)

            self.assertEqual(Path(result["project_dir"]), destination.resolve())
            self.assertTrue(result["output_explicitly_selected"])
            self.assertFalse(destination.exists())

    def test_explicit_task_request_allows_external_source_ingestion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / planner.ROOT_NAME
            root.mkdir()
            (root / planner.DATABASE_MARKER).write_text("{}", encoding="utf-8")
            source = Path(temporary) / "outside.md"
            source.write_text("成果", encoding="utf-8")

            unconfirmed = planner.build_output_plan(source, database_root=root)
            confirmed = planner.build_output_plan(
                source, database_root=root, ingest_confirmed=True
            )

            self.assertFalse(unconfirmed["ingest_allowed"])
            self.assertTrue(confirmed["ingest_allowed"])
            self.assertEqual(confirmed["ingest_authorization_basis"], "explicit_task_request")
            self.assertTrue(confirmed["automatic_backup_required"])
            self.assertFalse(confirmed["source_inside_database"])
            self.assertEqual(
                unconfirmed["ingest_authorization_basis"],
                "not_requested",
            )


if __name__ == "__main__":
    unittest.main()
