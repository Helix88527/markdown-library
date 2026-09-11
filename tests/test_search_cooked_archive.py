"""成稿库只读检索与相关性排序测试。"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "search_cooked_archive.py"
SPEC = importlib.util.spec_from_file_location("search_cooked_archive", SCRIPT)
assert SPEC and SPEC.loader
searcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(searcher)


class CookedArchiveSearchTests(unittest.TestCase):
    def test_relevant_history_ranks_before_unrelated_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cooked = root / "Markdown"
            cooked.mkdir()
            query = root / "02_校正逐字稿.md"
            query.write_text(
                "赵紫阳在十三大后的权力格局，以及1988年价格闯关和通货膨胀。",
                encoding="utf-8",
            )
            relevant = cooked / "赵紫阳下台前后.md"
            relevant.write_text(
                "# 赵紫阳下台前后\n十三大后的权力格局与1988年价格闯关。",
                encoding="utf-8",
            )
            unrelated = cooked / "黄金投资.md"
            unrelated.write_text(
                "# 黄金投资\n央行购金、矿产供应和黄金价格分析。",
                encoding="utf-8",
            )

            result = searcher.search_archive(query, cooked)

            self.assertEqual(Path(result["matches"][0]["path"]), relevant.resolve())
            self.assertTrue(result["read_only"])
            self.assertFalse(result["writes_performed"])
            self.assertIn("赵紫阳", result["matches"][0]["shared_terms"])
            self.assertEqual(result["matches"][0]["content_source"], "live-cooked-file")
            self.assertRegex(result["matches"][0]["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(result["matches"][0]["modified_at"].endswith("Z"))
            self.assertGreater(result["matches"][0]["size_bytes"], 0)
            self.assertIn("不使用持久缓存", result["snapshot_policy"])

    def test_query_and_excluded_file_are_not_returned_or_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cooked = Path(temporary) / "Markdown"
            cooked.mkdir()
            query = cooked / "当前节目.md"
            query.write_text("孙宇晨与景甜争议", encoding="utf-8")
            previous = cooked / "往期节目.md"
            previous.write_text("# 往期节目\n孙宇晨与景甜争议", encoding="utf-8")
            before = {path: path.read_bytes() for path in cooked.iterdir()}

            result = searcher.search_archive(query, cooked, excluded=[previous])

            self.assertEqual(result["candidate_count"], 0)
            self.assertEqual(result["matches"], [])
            self.assertEqual(before, {path: path.read_bytes() for path in cooked.iterdir()})

    def test_cooked_directory_must_remain_inside_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / searcher.ROOT_NAME
            toolkit = root / searcher.CONFIG_RELATIVE.parent
            toolkit.mkdir(parents=True)
            outside = base / "outside"
            outside.mkdir()
            (root / searcher.MARKER).write_text("{}", encoding="utf-8")
            (root / searcher.CONFIG_RELATIVE).write_text(
                '{"cooked_dir": "../outside"}', encoding="utf-8"
            )

            with self.assertRaises(searcher.SearchError):
                searcher.cooked_directory(root)

    def test_next_search_reads_latest_user_corrected_cooked_version(self) -> None:
        """成稿被人工改过后，下次回查不得复用旧内容或旧指纹。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cooked = root / "Markdown"
            cooked.mkdir()
            query = root / "02_校正逐字稿.md"
            query.write_text("价格闯关与通货膨胀", encoding="utf-8")
            history = cooked / "往期.md"
            history.write_text("# 往期\n价格闯关与通货膨胀。", encoding="utf-8")

            first = searcher.search_archive(query, cooked)["matches"][0]
            history.write_text(
                "# 往期（人工修订）\n价格闯关与通货膨胀，并补充工资改革。",
                encoding="utf-8",
            )
            revised_time = history.stat().st_mtime + 5
            os.utime(history, (revised_time, revised_time))
            second = searcher.search_archive(query, cooked)["matches"][0]

            self.assertNotEqual(first["sha256"], second["sha256"])
            self.assertNotEqual(first["modified_at_ns"], second["modified_at_ns"])
            self.assertEqual(second["title"], "往期（人工修订）")
            self.assertEqual(second["size_bytes"], len(history.read_bytes()))


if __name__ == "__main__":
    unittest.main()
