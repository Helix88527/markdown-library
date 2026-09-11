"""``visual_materials.py`` 的安全关卡、去重和 Word 结构测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "visual_materials.py"
SPEC = importlib.util.spec_from_file_location("visual_materials", SCRIPT)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - 测试环境损坏时给出明确错误。
    raise RuntimeError(f"无法导入 {SCRIPT}")
visual = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = visual
SPEC.loader.exec_module(visual)


class VisualMaterialsTests(unittest.TestCase):
    """验证写入确认、候选超限、dHash 去重与 DOCX 自包含结构。"""

    @staticmethod
    def make_image(path: Path, *, square_x: int, label: str) -> None:
        """建立信息结构可控的小图；不同方块位置会得到不同感知哈希。"""

        from PIL import Image, ImageDraw

        image = Image.new("RGB", (320, 180), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((square_x, 40, square_x + 70, 130), fill="black")
        draw.line((10, 150, 300, 150), fill="navy", width=4)
        draw.text((12, 12), label, fill="black")
        image.save(path)

    @staticmethod
    def base_extract_args(media: Path, output: Path) -> Namespace:
        """创建命令函数所需的完整参数集合，避免测试依赖 CLI 默认值。"""

        return Namespace(
            input=str(media),
            output_dir=str(output),
            manifest=None,
            confirmed_by_user=False,
            scene_threshold=visual.DEFAULT_SCENE_THRESHOLD,
            max_candidates=visual.DEFAULT_MAX_CANDIDATES,
            confirmed_candidate_count=None,
            start_seconds=0.0,
            end_seconds=None,
            dedup_hamming=visual.DEFAULT_DEDUP_HAMMING,
            min_information_score=visual.DEFAULT_MIN_INFORMATION_SCORE,
            keyframe_retention="keep",
            reference=[],
            ocr_language="chi_sim+eng",
            skip_ocr=True,
            json=True,
        )

    def test_extract_cli_requires_confirmation_before_any_probe_or_write(self) -> None:
        """缺少 --confirmed-by-user 时，连探测媒体都不应开始。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "sample.mp4"
            media.write_bytes(b"not-a-real-video")
            output = root / "visual-output"
            args = self.base_extract_args(media, output)
            with (
                mock.patch.object(visual, "find_ffmpeg") as forbidden_ffmpeg,
                self.assertRaises(visual.ConfirmationRequired),
            ):
                visual.command_extract(args)
            forbidden_ffmpeg.assert_not_called()
            self.assertFalse(output.exists())

    def test_build_docx_cli_requires_confirmation_before_reading_manifest(self) -> None:
        """build-docx 的确认关卡必须位于 manifest 读取之前。"""

        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "missing.json"
            args = Namespace(
                manifest=str(manifest),
                output=None,
                title=None,
                confirmed_by_user=False,
                json=True,
            )
            with (
                mock.patch.object(visual, "load_manifest") as forbidden_load,
                self.assertRaises(visual.ConfirmationRequired),
            ):
                visual.command_build_docx(args)
            forbidden_load.assert_not_called()

    def test_embed_markdown_cli_requires_confirmation_before_reading_manifest(self) -> None:
        """Markdown 图片写入也必须在读取和改写成果前通过确认门。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = Namespace(
                manifest=str(root / "missing.json"),
                markdown=str(root / "missing.md"),
                asset_dir=None,
                section_title="关键画面",
                confirmed_by_user=False,
                json=True,
            )
            with (
                mock.patch.object(visual, "load_manifest") as forbidden_load,
                self.assertRaises(visual.ConfirmationRequired),
            ):
                visual.command_embed_markdown(args)
            forbidden_load.assert_not_called()

    def test_inspect_is_read_only(self) -> None:
        """inspect 即使检查默认 manifest，也不能创建该文件或任何输出目录。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "sample.mp4"
            media.write_bytes(b"video")
            manifest = root / visual.DEFAULT_MANIFEST_NAME
            args = Namespace(
                input=str(media),
                manifest=None,
                scan_scenes=False,
                scene_threshold=visual.DEFAULT_SCENE_THRESHOLD,
                max_candidates=visual.DEFAULT_MAX_CANDIDATES,
                start_seconds=0.0,
                end_seconds=None,
                json=True,
            )
            with (
                mock.patch.object(visual, "find_ffmpeg", return_value=Path("ffmpeg")),
                mock.patch.object(visual, "probe_duration_seconds", return_value=60.0),
            ):
                result = visual.command_inspect(args)
            self.assertTrue(result["read_only"])
            self.assertFalse(result["manifest"]["exists"])
            self.assertFalse(manifest.exists())

    def test_keyframe_retention_is_explicit_and_defaults_to_keep(self) -> None:
        """独立截图默认保留，但用户可以登记为 Word 验证后白名单清理。"""

        parser = visual.build_parser()
        default_args = parser.parse_args(
            ["extract", "video.mp4", "--output-dir", "visual", "--confirmed-by-user"]
        )
        self.assertEqual(default_args.keyframe_retention, "keep")
        remove_args = parser.parse_args(
            [
                "extract",
                "video.mp4",
                "--output-dir",
                "visual",
                "--keyframe-retention",
                "remove_after_verified",
                "--confirmed-by-user",
            ]
        )
        self.assertEqual(remove_args.keyframe_retention, "remove_after_verified")

    def test_candidate_overflow_stops_before_creating_output(self) -> None:
        """候选超限时只报告，不得先创建输出目录或导出截图。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "sample.mp4"
            media.write_bytes(b"video")
            output = root / "visual-output"
            args = self.base_extract_args(media, output)
            args.confirmed_by_user = True
            args.max_candidates = 2
            with (
                mock.patch.object(visual, "find_ffmpeg", return_value=Path("ffmpeg")),
                mock.patch.object(visual, "probe_duration_seconds", return_value=60.0),
                mock.patch.object(
                    visual, "detect_scene_timestamps", return_value=[0.0, 10.0, 20.0]
                ),
                mock.patch.object(visual, "extract_visual_materials") as forbidden_extract,
                self.assertRaises(visual.CandidateOverflowError) as raised,
            ):
                visual.command_extract(args)
            forbidden_extract.assert_not_called()
            self.assertFalse(output.exists())
            self.assertIn('"detected_candidate_count": 3', str(raised.exception))
            self.assertIn("--confirmed-candidate-count", str(raised.exception))

    def test_candidate_overflow_accepts_exact_confirmed_count(self) -> None:
        """超限后只能用本次检测到的精确数量确认，陈旧数量仍会阻止。"""

        timestamps = [0.0, 10.0, 20.0]
        with self.assertRaises(visual.CandidateOverflowError):
            visual.enforce_candidate_limit(
                timestamps,
                max_candidates=2,
                confirmed_candidate_count=4,
                start=0.0,
                end=30.0,
            )
        visual.enforce_candidate_limit(
            timestamps,
            max_candidates=2,
            confirmed_candidate_count=3,
            start=0.0,
            end=30.0,
        )

    def test_perceptual_dedup_keeps_first_near_identical_image(self) -> None:
        """完全相同画面去重，明显不同版式继续保留。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.png"
            duplicate = root / "duplicate.png"
            distinct = root / "distinct.png"
            self.make_image(first, square_x=30, label="same")
            duplicate.write_bytes(first.read_bytes())
            self.make_image(distinct, square_x=210, label="different")

            unique, duplicates = visual.deduplicate_image_paths(
                [first, duplicate, distinct], max_hamming_distance=1
            )
            self.assertEqual(unique, [first, distinct])
            self.assertEqual(duplicates, {duplicate: first})

    def _create_complete_manifest(self, root: Path) -> Path:
        """建立含两张已复核截图的最小合法 manifest。"""

        image_dir = root / visual.FINAL_SCREENSHOT_DIR
        image_dir.mkdir()
        first = image_dir / "frame-0001.png"
        second = image_dir / "frame-0002.png"
        self.make_image(first, square_x=25, label="slide one")
        self.make_image(second, square_x=210, label="slide two")
        items = []
        for index, image in enumerate((first, second), start=1):
            items.append(
                {
                    "item_id": f"visual-{index:04d}",
                    "candidate_index": index,
                    "timestamp_seconds": float(index * 10),
                    "timestamp": visual.format_timestamp(index * 10),
                    "screenshot_path": image.relative_to(root).as_posix(),
                    "screenshot_sha256": visual.sha256_file(image),
                    "perceptual_hash": visual.perceptual_hash(image),
                    "selection_reason": "test",
                    "visual_kind": "PPT，测试中已人工确认",
                    "ocr_raw": f"OCR 原文 {index}",
                    "ocr_engine": "test",
                    "corrected_text": f"校正文字 {index}",
                    "references": [f"参考资料 {index}.md"],
                    "related_transcript": f"逐字稿第 {index} 段",
                    "related_theme": f"主题 {index}",
                    "reference_findings": f"参考资料支持的事实 {index}",
                    "description": f"可见画面说明 {index}",
                    "inference": f"整理者推断 {index}",
                    "uncertainties": [f"待核字符 {index}"],
                    "review_status": "complete",
                }
            )
        manifest = {
            "schema_version": visual.SCHEMA_VERSION,
            "workflow_version": visual.WORKFLOW_VERSION,
            "created_at": visual.now_iso(),
            "updated_at": visual.now_iso(),
            "source": {
                "path": str(root / "sample.mp4"),
                "name": "sample.mp4",
                "size_bytes": 5,
                "mtime_ns": 1,
                "sha256": "0" * 64,
                "duration_seconds": 60.0,
            },
            "extraction": {
                "status": "review_complete",
                "signature": {
                    "start_seconds": 0.0,
                    "end_seconds": 60.0,
                    "scene_threshold": 0.28,
                    "dedup_hamming_distance": 5,
                    "min_information_score": 0.015,
                    "candidate_timestamps": [10.0, 20.0],
                },
            },
            "reference_materials": [],
            "candidate_frames": [],
            "items": items,
            "cleanup_plan": {
                "default_policy": "测试计划",
                "automatic_deletion": False,
                "status": "planned",
                "owned_temporary_files": [".visual-candidates/known.png"],
                "permanent_files": [item["screenshot_path"] for item in items],
                "unknown_file_policy": "block_and_report",
            },
            "document": None,
        }
        manifest_path = root / visual.DEFAULT_MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest_path

    def test_build_docx_rejects_pending_review_before_output(self) -> None:
        """任一条目未 complete 时不得产生半成品 Word。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = self._create_complete_manifest(root)
            manifest = visual.load_manifest(manifest_path, verify_images=True)
            manifest["items"][1]["review_status"] = "pending"
            output = root / visual.DEFAULT_DOCX_NAME
            with self.assertRaisesRegex(visual.VisualWorkflowError, "review_status=complete"):
                visual.build_visual_docx(manifest, manifest_path, output, title="测试视觉资料")
            self.assertFalse(output.exists())

    def test_complete_status_requires_all_separated_review_fields(self) -> None:
        """complete 不能掩盖尚未填写的逐字稿、主题、证据或推断字段。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = self._create_complete_manifest(root)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["items"][0]["inference"] = ""
            manifest_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            with self.assertRaisesRegex(visual.VisualWorkflowError, "整理者推断"):
                visual.load_manifest(manifest_path, verify_images=True)

    def test_build_docx_embeds_images_and_required_fields(self) -> None:
        """Word 必须包含两张内联截图和全部复核字段，且不依赖外部图片。"""

        from docx import Document

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = self._create_complete_manifest(root)
            manifest = visual.load_manifest(manifest_path, verify_images=True)
            output = root / visual.DEFAULT_DOCX_NAME
            visual.build_visual_docx(manifest, manifest_path, output, title="测试视觉资料")

            document = Document(output)
            self.assertEqual(len(document.inline_shapes), 2)
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            for expected in (
                "时间戳：00:00:10.000",
                "OCR 原始识别文字：OCR 原文 1",
                "校正后的画面文字：校正文字 1",
                "参考资料：参考资料 1.md",
                "关联逐字稿：逐字稿第 1 段",
                "关联主题：主题 1",
                "参考资料核验信息：参考资料支持的事实 1",
                "可见画面说明：可见画面说明 1",
                "整理者推断：整理者推断 1",
                "不确定项：待核字符 1",
            ):
                self.assertIn(expected, text)

            with zipfile.ZipFile(output) as package:
                media_parts = [name for name in package.namelist() if name.startswith("word/media/")]
                relationships = package.read("word/_rels/document.xml.rels").decode("utf-8")
            self.assertEqual(len(media_parts), 2)
            self.assertNotIn('TargetMode="External"', relationships)

    def test_build_command_updates_cleanup_plan_but_deletes_nothing(self) -> None:
        """成功构建只推进清理计划状态，不自动删除已登记或未知文件。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = self._create_complete_manifest(root)
            temp_dir = root / visual.TEMP_SCREENSHOT_DIR
            temp_dir.mkdir()
            known = temp_dir / "known.png"
            unknown = temp_dir / "manual-note.txt"
            known.write_bytes(b"known")
            unknown.write_text("必须保留", encoding="utf-8")
            output = root / visual.DEFAULT_DOCX_NAME
            args = Namespace(
                manifest=str(manifest_path),
                output=str(output),
                title="测试视觉资料",
                confirmed_by_user=True,
                json=True,
            )
            result = visual.command_build_docx(args)

            self.assertTrue(output.is_file())
            self.assertTrue(known.is_file())
            self.assertTrue(unknown.is_file())
            self.assertFalse(result["cleanup_performed"])
            reloaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertFalse(reloaded["cleanup_plan"]["automatic_deletion"])
            self.assertEqual(
                reloaded["cleanup_plan"]["status"], "awaiting_docx_render_verification"
            )

    def test_embed_markdown_copies_named_assets_and_is_idempotent(self) -> None:
        """最终 Markdown 使用相对图片路径；重跑只替换受管章节。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = self._create_complete_manifest(root)
            markdown = root / "sample.md"
            markdown.write_text("# 研究记录\n\n人工正文。\n", encoding="utf-8")
            args = Namespace(
                manifest=str(manifest_path),
                markdown=str(markdown),
                asset_dir=None,
                section_title="关键画面",
                confirmed_by_user=True,
                json=True,
            )

            first = visual.command_embed_markdown(args)
            second = visual.command_embed_markdown(args)

            self.assertEqual(first["asset_count"], 2)
            self.assertTrue(second["managed_section_replaced"])
            assets = root / "sample_assets"
            names = sorted(path.name for path in assets.glob("*.png"))
            self.assertEqual(len(names), 2)
            self.assertTrue(all(name.startswith("sample_00-00-") for name in names))
            text = markdown.read_text(encoding="utf-8")
            self.assertEqual(text.count(visual.MARKDOWN_VISUALS_START), 1)
            self.assertEqual(text.count(visual.MARKDOWN_VISUALS_END), 1)
            self.assertIn("人工正文。", text)
            self.assertIn("sample_assets/sample_", text)
            self.assertNotIn(str(root), text)

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["markdown_visuals"]["assets"]), 2)

    def test_embed_markdown_refuses_conflicting_asset_before_edit(self) -> None:
        """同名图片内容不同时停止，保留原 Markdown 和冲突图片。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = self._create_complete_manifest(root)
            manifest = visual.load_manifest(manifest_path, verify_images=True)
            markdown = root / "sample.md"
            original = "# 研究记录\n\n人工正文。\n"
            markdown.write_text(original, encoding="utf-8")
            assets = root / "sample_assets"
            assets.mkdir()
            first_item = manifest["items"][0]
            source_image = visual.resolve_manifest_path(
                first_item["screenshot_path"], manifest_path.parent
            )
            conflict_name = visual._markdown_asset_filename(
                manifest["source"]["name"], first_item, suffix=source_image.suffix
            )
            conflict = assets / conflict_name
            conflict.write_bytes(b"different")

            with self.assertRaisesRegex(visual.VisualWorkflowError, "拒绝覆盖"):
                visual.embed_markdown_visuals(manifest, manifest_path, markdown)

            self.assertEqual(markdown.read_text(encoding="utf-8"), original)
            self.assertEqual(conflict.read_bytes(), b"different")


if __name__ == "__main__":
    unittest.main()
