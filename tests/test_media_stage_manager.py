"""不加载 Whisper 模型的阶段管理与安全复制单元测试。"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, path: Path):
    """从 Skill 脚本目录加载模块，不依赖安装为 Python 包。"""

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载测试模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manager = load_module("media_stage_manager", SKILL_ROOT / "scripts" / "media_stage_manager.py")
safe_copy = load_module("safe_copy_to_cooked", SKILL_ROOT / "scripts" / "safe_copy_to_cooked.py")
fidelity = load_module("audit_source_fidelity", SKILL_ROOT / "scripts" / "audit_source_fidelity.py")


def write_valid_coverage_report(corrected: Path, final: Path) -> Path:
    report = corrected.parent / manager.HANDOFF_DIRNAME / manager.COVERAGE_REPORT_FILENAME
    payload = fidelity.prepare_report(corrected, report, max_chars=400)
    payload["units"][0]["information_units"] = [
        {
            "kind": "claim",
            "source_detail": "校正稿记录了一个需要完整保留的具体主张",
            "final_evidence": "最终研究记录完整保留了这个具体主张",
        }
    ]
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


class PlanningTests(unittest.TestCase):
    """验证一个半小时边界、分段覆盖和计划稳定性。"""

    def test_ninety_minute_boundary_is_strict(self) -> None:
        self.assertEqual(manager.WORKFLOW_VERSION, (SKILL_ROOT / "VERSION").read_text().strip())
        self.assertEqual(manager.LONG_MEDIA_THRESHOLD_MS, 5_400_000)
        self.assertFalse(5_400_000 > manager.LONG_MEDIA_THRESHOLD_MS)
        self.assertTrue(5_400_001 > manager.LONG_MEDIA_THRESHOLD_MS)
        self.assertEqual(manager.suggest_chunk_count(5_400_001), 2)

    def test_exactly_ninety_minutes_uses_short_media_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / manager.ROOT_NAME
            task = root / "原始资料" / "视频资料" / "九十分钟边界"
            task.mkdir(parents=True)
            media = task / "九十分钟.mp4"
            media.write_bytes(b"media")
            with mock.patch.object(manager, "probe_duration_ms", return_value=5_400_000):
                payload = manager.inspect_payload(media, root, 60, 10)
            self.assertFalse(payload["is_over_long_media_threshold"])
            self.assertFalse(payload["is_over_ninety_minutes"])
            self.assertIsNone(payload["suggested_chunk_count"])
            self.assertEqual(payload["suggested_ranges"], [])

    def test_one_millisecond_over_threshold_keeps_balanced_overlap_plan(self) -> None:
        duration = 5_400_001
        plan = manager.build_chunk_plan(
            duration,
            manager.suggest_chunk_count(duration),
            manager.DEFAULT_OVERLAP_SECONDS * 1000,
        )
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0]["core_end_ms"], plan[1]["core_start_ms"])
        self.assertEqual(plan[0]["decode_end_ms"] - plan[0]["core_end_ms"], 10_000)
        self.assertEqual(plan[1]["core_start_ms"] - plan[1]["decode_start_ms"], 10_000)

    def test_chunk_plan_covers_core_without_gap(self) -> None:
        plan = manager.build_chunk_plan(13_000_123, 4, 10_000)
        self.assertEqual(plan[0]["core_start_ms"], 0)
        self.assertEqual(plan[-1]["core_end_ms"], 13_000_123)
        for left, right in zip(plan, plan[1:]):
            self.assertEqual(left["core_end_ms"], right["core_start_ms"])
            self.assertGreater(left["decode_end_ms"], left["core_end_ms"])
            self.assertLess(right["decode_start_ms"], right["core_start_ms"])
        self.assertEqual(manager.plan_hash(plan), manager.plan_hash(plan))


class MergeTests(unittest.TestCase):
    """验证边界去重仅作用于跨段、重叠且高度相似的句子。"""

    @staticmethod
    def segment(index: int, start: int, end: int, text: str, score: float = -0.3) -> dict:
        return {
            "id": 1,
            "start": start / 1000,
            "end": end / 1000,
            "start_ms": start,
            "end_ms": end,
            "text": text,
            "avg_logprob": score,
            "no_speech_prob": 0.1,
            "compression_ratio": 1.0,
            "chunk_index": index,
        }

    def test_cross_chunk_overlap_duplicate_is_removed(self) -> None:
        first = self.segment(1, 59_000, 61_000, "这是切点附近的同一句话", -0.4)
        better = self.segment(2, 59_200, 61_100, "这是切点附近的同一句话。", -0.2)
        later_repeat = self.segment(2, 80_000, 82_000, "这是切点附近的同一句话", -0.1)
        merged, warnings = manager.merge_segments([{"segments": [first]}, {"segments": [better, later_repeat]}])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["chunk_index"], 2)
        self.assertEqual(merged[1]["start_ms"], 80_000)
        self.assertEqual([item["id"] for item in merged], [1, 2])
        self.assertEqual(warnings, [])

    def test_uncertain_boundary_is_kept_and_warned(self) -> None:
        left = self.segment(1, 59_000, 61_000, "赵先生后来提到这一件事情的背景")
        right = self.segment(2, 60_000, 62_000, "赵先生后来补充了这件事情的另一层背景")
        merged, warnings = manager.merge_segments([{"segments": [left]}, {"segments": [right]}])
        self.assertEqual(len(merged), 2)
        self.assertGreaterEqual(len(warnings), 1)

    def test_bundle_validation_does_not_count_numeric_transcript_as_srt_id(self) -> None:
        """字幕正文只有数字时，不得被误算成额外的 SRT 编号。"""

        segments = [
            {**self.segment(1, 1_000, 2_000, "2024"), "id": 1},
            {**self.segment(1, 2_500, 3_500, "下一句话"), "id": 2},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "01_原始逐字稿"
            paths = {
                "md": base.with_suffix(".md"),
                "srt": base.with_suffix(".srt"),
                "json": base.with_suffix(".json"),
                "jsonl": base.with_suffix(".jsonl"),
            }
            paths["md"].write_text(
                manager.render_transcript_markdown("测试", [], segments), encoding="utf-8"
            )
            paths["srt"].write_text(manager.render_srt(segments), encoding="utf-8")
            paths["json"].write_text(
                json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8"
            )
            paths["jsonl"].write_text(manager.render_jsonl(segments), encoding="utf-8")

            manager.validate_bundle(paths, expected_count=2)


class PreviousContextTests(unittest.TestCase):
    """验证提示配额和旧状态兼容不会造成静默截断或追溯迁移。"""

    class FakeEncoding:
        def __init__(self, ids):
            self.ids = ids

    class FakeTokenizer:
        def encode(self, text):
            return PreviousContextTests.FakeEncoding([ord(character) for character in text])

        def decode(self, ids):
            return "".join(chr(item) for item in ids)

    def test_prompt_budget_keeps_terms_and_previous_context(self) -> None:
        model = SimpleNamespace(hf_tokenizer=self.FakeTokenizer(), max_length=100)
        prompt, metrics = manager.prepare_bounded_initial_prompt(
            model,
            "术语" * 80,
            "前文" * 80,
        )
        self.assertLessEqual(metrics["prompt_token_count"], 49)
        self.assertGreater(metrics["terminology_tokens_used"], 0)
        self.assertGreater(metrics["context_tokens_used"], 0)
        self.assertIn("前文", prompt)

    def test_legacy_state_does_not_enable_new_destructive_policy(self) -> None:
        state = {"execution": {}, "stage1": {}}
        self.assertFalse(manager.previous_context_policy(state)["enabled"])
        self.assertFalse(manager.stage1_compaction_required(state))
        self.assertEqual(manager.stage1_compaction_status(state), "deferred")


class AtomicAndCleanupTests(unittest.TestCase):
    """验证原子写入和未知文件保护。"""

    def test_atomic_text_replaces_complete_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "状态.json"
            manager.atomic_write_text(path, "旧内容")
            manager.atomic_write_text(path, "新内容")
            self.assertEqual(path.read_text(encoding="utf-8"), "新内容")
            self.assertEqual(list(path.parent.glob(".*.tmp.*")), [])

    def test_missing_coverage_report_blocks_fidelity_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corrected = root / "02_校正逐字稿.md"
            final = root / "节目.md"
            corrected.write_text("这里有一个必须保留的具体主张。", encoding="utf-8")
            final.write_text("成稿完整保留了这个具体主张。", encoding="utf-8")
            with self.assertRaisesRegex(manager.WorkflowError, "覆盖校验失败"):
                manager.run_fidelity_audit(corrected, final, root / "不存在.json")

    def test_unknown_file_blocks_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / manager.ROOT_NAME
            task = root / "原始资料" / "视频资料" / "测试"
            task.mkdir(parents=True)
            (root / manager.DATABASE_MARKER).write_text("{}", encoding="utf-8")
            media = task / "测试.mp4"
            media.write_bytes(b"media")
            handoff = task / manager.HANDOFF_DIRNAME
            parts = task / manager.PARTS_DIRNAME
            handoff.mkdir()
            parts.mkdir()
            state_file = handoff / manager.STATE_FILENAME
            progress = handoff / manager.PROGRESS_FILENAME
            manifest = parts / manager.MANIFEST_FILENAME
            for path in (state_file, progress, manifest):
                path.write_text("{}", encoding="utf-8")
            state = {
                "temporary_owned_files": [
                    manager.relative_to_database(path, root) for path in (state_file, progress, manifest)
                ]
            }
            self.assertEqual(manager.find_unknown_temporary_entries(state, media, root), [])
            note = handoff / "我的人工备注.md"
            note.write_text("保留", encoding="utf-8")
            unknown = manager.find_unknown_temporary_entries(state, media, root)
            self.assertIn(manager.relative_to_database(note, root), unknown)

    def test_cleanup_whitelist_cannot_target_permanent_file(self) -> None:
        """损坏状态不得借清理白名单删除临时目录之外的永久成果。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / manager.ROOT_NAME
            task = root / "原始资料" / "视频资料" / "清理边界"
            handoff = task / manager.HANDOFF_DIRNAME
            parts = task / manager.PARTS_DIRNAME
            handoff.mkdir(parents=True)
            parts.mkdir()
            media = task / "清理边界.mp4"
            permanent = task / "02_校正逐字稿.md"
            media.write_bytes(b"media")
            permanent.write_text("永久保留", encoding="utf-8")
            state_path = handoff / manager.STATE_FILENAME
            progress = handoff / manager.PROGRESS_FILENAME
            manifest = parts / manager.MANIFEST_FILENAME
            for path in (state_path, progress, manifest):
                path.write_text("{}", encoding="utf-8")
            state = {
                "temporary_owned_files": [
                    manager.relative_to_database(path, root)
                    for path in (state_path, progress, manifest, permanent)
                ]
            }
            with self.assertRaisesRegex(manager.WorkflowError, "不属于当前任务临时目录"):
                manager.cleanup_temporary_state(state, media, root)
            self.assertTrue(permanent.is_file())
            self.assertEqual(permanent.read_text(encoding="utf-8"), "永久保留")


class InspectStateTests(unittest.TestCase):
    """验证预检不会把损坏或错配的旧状态误称为可续跑断点。"""

    @staticmethod
    def create_paths(temporary: str) -> tuple[Path, Path, Path]:
        root = Path(temporary) / manager.ROOT_NAME
        task = root / "原始资料" / "视频资料" / "预检"
        handoff = task / manager.HANDOFF_DIRNAME
        handoff.mkdir(parents=True)
        media = task / "预检.wav"
        media.write_bytes(b"test-media")
        return root, media, handoff / manager.STATE_FILENAME

    @staticmethod
    def valid_state(root: Path, media: Path, state_path: Path) -> dict:
        """构造一份最小但完整、可续跑的 staged 第一阶段完成状态。"""

        stat = media.stat()
        chunks = [
            {
                "index": 1,
                "core_start_ms": 0,
                "core_end_ms": 1_000,
                "decode_start_ms": 0,
                "decode_end_ms": 1_000,
                "status": "complete",
            }
        ]
        return {
            "schema_version": manager.SCHEMA_VERSION,
            "workflow_id": "workflow-test",
            "source": {
                "path": manager.relative_to_database(media, root),
                "duration_ms": 1_000,
                "identity": {
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": manager.sha256_file(media),
                },
            },
            "execution": {
                "mode": "staged",
                "chunk_count": 1,
                "plan_hash": manager.plan_hash(chunks),
            },
            "stage1": {
                "status": "complete",
                "gate": {"kind": "enter_stage2"},
                "chunks": chunks,
                "merge": {"status": "complete"},
            },
            "stage2": {"status": "pending"},
            "temporary_owned_files": [manager.relative_to_database(state_path, root)],
        }

    def test_malformed_state_is_reported_as_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, media, state_path = self.create_paths(temporary)
            state_path.write_text("{不是有效 JSON", encoding="utf-8")
            existing, issue = manager.inspect_existing_workflow(media, root, state_path)
            self.assertIsNone(existing)
            self.assertEqual(issue["kind"], "invalid_existing_workflow")
            self.assertIn(manager.STATE_FILENAME, issue["state_path"])

    def test_state_for_another_source_is_not_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, media, state_path = self.create_paths(temporary)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": manager.SCHEMA_VERSION,
                        "workflow_id": "wrong-source",
                        "source": {"path": "别的媒体.wav", "identity": {}},
                        "execution": {"mode": "staged"},
                        "stage1": {},
                        "stage2": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            existing, issue = manager.inspect_existing_workflow(media, root, state_path)
            self.assertIsNone(existing)
            self.assertIn("状态来源", issue["message"])

    def test_matching_state_is_reported_as_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, media, state_path = self.create_paths(temporary)
            state = self.valid_state(root, media, state_path)
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            existing, issue = manager.inspect_existing_workflow(media, root, state_path)
            self.assertIsNone(issue)
            self.assertEqual(existing["workflow_id"], "workflow-test")
            self.assertIn("等待用户确认", existing["next_action"])

    def test_empty_chunk_plan_is_reported_as_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, media, state_path = self.create_paths(temporary)
            state = self.valid_state(root, media, state_path)
            state["execution"].update({"chunk_count": 0, "plan_hash": manager.plan_hash([])})
            state["stage1"].update(
                {"status": "ready", "gate": None, "chunks": [], "merge": {"status": "pending"}}
            )
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            existing, issue = manager.inspect_existing_workflow(media, root, state_path)
            self.assertIsNone(existing)
            self.assertIn("chunks", issue["message"])

    def test_malformed_user_gate_is_reported_as_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, media, state_path = self.create_paths(temporary)
            state = self.valid_state(root, media, state_path)
            state["stage1"].update(
                {
                    "status": "awaiting_continue",
                    "gate": "损坏的关卡",
                    "chunks": [{**state["stage1"]["chunks"][0], "status": "pending"}],
                    "merge": {"status": "pending"},
                }
            )
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            existing, issue = manager.inspect_existing_workflow(media, root, state_path)
            self.assertIsNone(existing)
            self.assertIn("用户关卡", issue["message"])


class MaterialDiscoveryTests(unittest.TestCase):
    """验证同项目清单只读，并把多资料决策明确留给用户。"""

    @staticmethod
    def create_project(temporary: str) -> tuple[Path, Path]:
        root = Path(temporary) / manager.ROOT_NAME
        project = root / "原始资料" / "视频资料" / "多资料项目"
        project.mkdir(parents=True)
        return root, project

    def test_inventory_lists_media_and_transcript_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, project = self.create_project(temporary)
            (project / "节目.mp4").write_bytes(b"video")
            (project / "节目.mp3").write_bytes(b"audio")
            (project / "下载软件逐字稿.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n内容\n",
                encoding="utf-8",
            )
            (project / "普通配置.json").write_text('{"enabled": true}', encoding="utf-8")
            before = sorted(path.name for path in project.iterdir())

            inventory = manager.discover_project_materials(project, root)

            after = sorted(path.name for path in project.iterdir())
            self.assertEqual(before, after)
            self.assertTrue(inventory["read_only"])
            self.assertEqual(inventory["counts"], {"videos": 1, "audios": 1, "transcripts": 1})
            self.assertTrue(inventory["user_choice"]["required"])
            self.assertIsNone(inventory["user_choice"]["selected_option_id"])
            self.assertFalse(inventory["user_choice"]["automatic_selection_allowed"])
            option_ids = {item["id"] for item in inventory["user_choice"]["options"]}
            self.assertIn("reuse_transcript_selective_verification", option_ids)
            self.assertNotIn(
                "普通配置.json",
                [item["name"] for item in inventory["materials"]["transcripts"]],
            )

    def test_project_inspection_does_not_choose_between_video_and_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, project = self.create_project(temporary)
            (project / "节目.mp4").write_bytes(b"video")
            (project / "节目.wav").write_bytes(b"audio")
            with mock.patch.object(manager, "probe_duration_ms") as forbidden_probe:
                payload = manager.inspect_project_payload(project, root, 60, 10)
            forbidden_probe.assert_not_called()
            self.assertIsNone(payload["source"])
            self.assertTrue(payload["requires_user_material_choice"])
            self.assertIsNone(payload["material_inventory"]["user_choice"]["selected_option_id"])

    def test_legacy_extracted_audio_is_listed_but_not_treated_as_independent_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, project = self.create_project(temporary)
            (project / "节目.mp4").write_bytes(b"video")
            (project / "节目_音频.m4a").write_bytes(b"derived-audio")
            inventory = manager.discover_project_materials(project, root)
            self.assertEqual(inventory["counts"]["audios"], 1)
            self.assertEqual(inventory["decision_counts"]["independent_audios"], 0)
            self.assertFalse(inventory["user_choice"]["required"])
            self.assertEqual(
                inventory["materials"]["audios"][0]["origin_hint"],
                "workflow_extracted_audio",
            )

    def test_multiple_transcripts_are_never_auto_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, project = self.create_project(temporary)
            (project / "甲逐字稿.txt").write_text("甲版本", encoding="utf-8")
            (project / "乙逐字稿.md").write_text("乙版本", encoding="utf-8")
            inventory = manager.discover_project_materials(project, root)
            self.assertIn(
                "multiple_transcript_candidates",
                inventory["user_choice"]["reason_codes"],
            )
            self.assertFalse(inventory["user_choice"]["automatic_transcript_merge_allowed"])
            with self.assertRaisesRegex(manager.WorkflowError, "拒绝自动选择或合并"):
                manager.select_external_transcript(project, root)


class ExternalTranscriptImportTests(unittest.TestCase):
    """验证外部稿原件保护、时间戳保留和不伪造不存在的格式。"""

    @staticmethod
    def create_project(temporary: str) -> tuple[Path, Path]:
        root = Path(temporary) / manager.ROOT_NAME
        project = root / "原始资料" / "视频资料" / "外部逐字稿"
        project.mkdir(parents=True)
        return root, project

    def test_txt_import_preserves_source_and_does_not_fabricate_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, project = self.create_project(temporary)
            source = project / "软件导出的逐字稿.txt"
            source.write_text("第一段\r\n第二段", encoding="utf-8")
            original_bytes = source.read_bytes()

            result = manager.import_external_transcript(
                source,
                project,
                root,
                source_software="示例下载软件",
            )

            self.assertEqual(source.read_bytes(), original_bytes)
            output = project / "01_原始逐字稿.md"
            manifest_path = project / manager.FINAL_BUNDLE_MANIFEST
            self.assertTrue(output.is_file())
            self.assertTrue(manifest_path.is_file())
            self.assertFalse((project / "01_原始逐字稿.srt").exists())
            self.assertFalse((project / "01_原始逐字稿.json").exists())
            self.assertFalse((project / "01_原始逐字稿.jsonl").exists())
            markdown = output.read_text(encoding="utf-8-sig")
            self.assertIn("原文件没有可识别时间戳；未伪造", markdown)
            self.assertNotRegex(markdown, r"\*\*\[\d{2}:\d{2}:\d{2}")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["route"], "external_transcript_import")
            self.assertEqual(manifest["normalization"]["generated_formats"], ["md"])
            self.assertEqual(
                manifest["normalization"]["timestamps"]["status"],
                "absent_not_fabricated",
            )
            self.assertEqual(result["status"], "imported")

    def test_utf16_txt_import_records_encoding_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, project = self.create_project(temporary)
            source = project / "Windows软件逐字稿.txt"
            source.write_text("UTF-16 中文逐字稿", encoding="utf-16")
            source_bytes = source.read_bytes()
            manager.import_external_transcript(source, project, root)
            self.assertEqual(source.read_bytes(), source_bytes)
            markdown = (project / "01_原始逐字稿.md").read_text(encoding="utf-8-sig")
            self.assertIn("UTF-16 中文逐字稿", markdown)
            manifest = json.loads(
                (project / manager.FINAL_BUNDLE_MANIFEST).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["source"]["encoding"], "utf-16-bom")

    def test_srt_import_preserves_millisecond_timestamps_and_multiline_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, project = self.create_project(temporary)
            source = project / "字幕.srt"
            source.write_text(
                "1\n00:00:01,125 --> 00:00:03,875\n第一行\n第二行\n\n"
                "2\n00:01:04,010 --> 00:01:05,020\n下一段\n",
                encoding="utf-8",
            )
            before_hash = manager.sha256_file(source)

            manager.import_external_transcript(source, project, root)

            self.assertEqual(manager.sha256_file(source), before_hash)
            markdown = (project / "01_原始逐字稿.md").read_text(encoding="utf-8-sig")
            self.assertIn("[00:00:01.125–00:00:03.875]", markdown)
            self.assertIn("第一行<br>第二行", markdown)
            self.assertIn("[00:01:04.010–00:01:05.020]", markdown)
            manifest = json.loads(
                (project / manager.FINAL_BUNDLE_MANIFEST).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["normalization"]["timestamps"]["status"], "preserved")
            self.assertEqual(manifest["normalization"]["timestamps"]["cue_count"], 2)

    def test_vtt_import_preserves_cue_identifier_timing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, project = self.create_project(temporary)
            source = project / "字幕.vtt"
            source.write_text(
                "WEBVTT\nKind: captions\n\n"
                "cue-one\n00:02.250 --> 00:04.500 align:start\nVTT 内容\n",
                encoding="utf-8",
            )
            manager.import_external_transcript(source, project, root)
            markdown = (project / "01_原始逐字稿.md").read_text(encoding="utf-8-sig")
            self.assertIn("[00:00:02.250–00:00:04.500]", markdown)
            self.assertIn("VTT 内容", markdown)

    def test_import_never_overwrites_existing_normalized_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, project = self.create_project(temporary)
            source = project / "逐字稿.txt"
            source.write_text("原始机器稿", encoding="utf-8")
            output = project / "01_原始逐字稿.md"
            output.write_text("人工保留内容", encoding="utf-8")
            with self.assertRaisesRegex(manager.WorkflowError, "拒绝覆盖"):
                manager.import_external_transcript(source, project, root)
            self.assertEqual(output.read_text(encoding="utf-8"), "人工保留内容")
            self.assertEqual(source.read_text(encoding="utf-8"), "原始机器稿")


class SafeCopyTests(unittest.TestCase):
    """验证同哈希复用、冲突保护和原子替换。"""

    def test_cli_requires_explicit_ingest_confirmation(self) -> None:
        """执行入库仍须由调用方传入已有任务授权。"""

        parser = safe_copy.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["成果.md"])
        confirmed = parser.parse_args(["成果.md", "--confirmed-by-user"])
        self.assertTrue(confirmed.confirmed_by_user)
        replacement = parser.parse_args(
            [
                "成果.md",
                "--confirmed-by-user",
                "--replace",
                "--replace-confirmed-by-user",
            ]
        )
        self.assertTrue(replacement.replace)
        self.assertTrue(replacement.replace_confirmed_by_user)

    def test_resolve_source_accepts_visual_docx_but_rejects_other_formats(self) -> None:
        """Word 视觉附件复用 Markdown 的资料库边界，不放宽为任意文件。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / safe_copy.ROOT_NAME
            config_path = root / safe_copy.CONFIG_RELATIVE
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({"cooked_dir": "Markdown"}, ensure_ascii=False),
                encoding="utf-8",
            )
            visual = root / "原始资料" / "项目" / "03_视觉资料.docx"
            visual.parent.mkdir(parents=True)
            visual.write_bytes(b"valid-docx-placeholder")
            source, resolved_root = safe_copy.resolve_source(visual, root)
            self.assertEqual(source, visual.resolve())
            self.assertEqual(resolved_root, root.resolve())

            executable = visual.with_suffix(".exe")
            executable.write_bytes(b"not-an-artifact")
            with self.assertRaises(safe_copy.CopyError):
                safe_copy.resolve_source(executable, root)

    def test_explicit_database_root_allows_external_completed_artifact(self) -> None:
        """下载目录最终稿可自动备份，但目标边界仍由 cooked_dir 控制。"""

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / safe_copy.ROOT_NAME
            config_path = root / safe_copy.CONFIG_RELATIVE
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({"cooked_dir": "Markdown"}, ensure_ascii=False),
                encoding="utf-8",
            )
            external = base / "下载" / "完成稿.md"
            external.parent.mkdir()
            external.write_text("成果", encoding="utf-8")

            source, resolved_root = safe_copy.resolve_source(external, root)

            self.assertEqual(source, external.resolve())
            self.assertEqual(resolved_root, root.resolve())

    def test_atomic_copy_and_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "来源.md"
            destination = root / "成稿" / "来源.md"
            source.write_text("第一版", encoding="utf-8")
            first = safe_copy.copy_atomic(source, destination, replace=False)
            self.assertEqual(first["status"], "copied")
            second = safe_copy.copy_atomic(source, destination, replace=False)
            self.assertEqual(second["status"], "unchanged")
            source.write_text("第二版", encoding="utf-8")
            with self.assertRaises(safe_copy.CopyError):
                safe_copy.copy_atomic(source, destination, replace=False)
            with self.assertRaisesRegex(safe_copy.CopyError, "独立明确确认"):
                safe_copy.copy_atomic(source, destination, replace=True)
            replaced = safe_copy.copy_atomic(
                source,
                destination,
                replace=True,
                replace_confirmed_by_user=True,
            )
            self.assertEqual(replaced["status"], "copied")
            self.assertEqual(destination.read_text(encoding="utf-8"), "第二版")
            self.assertEqual(safe_copy.sha256_file(source), safe_copy.sha256_file(destination))

    def test_automatic_copy_never_overwrites_user_corrected_cooked_file(self) -> None:
        """入库授权不等于允许覆盖成稿中的人工修订。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "项目完成稿.md"
            destination = root / "成稿" / "项目完成稿.md"
            source.write_text("项目目录版本", encoding="utf-8")
            destination.parent.mkdir()
            destination.write_text("成稿库人工修订版本", encoding="utf-8")
            before_hash = safe_copy.sha256_file(destination)

            with self.assertRaisesRegex(safe_copy.CopyError, "可能包含用户手工修订"):
                safe_copy.copy_atomic(source, destination, replace=False)

            self.assertEqual(destination.read_text(encoding="utf-8"), "成稿库人工修订版本")
            self.assertEqual(safe_copy.sha256_file(destination), before_hash)


class StateRenderingTests(unittest.TestCase):
    """验证交接说明包含用户下次真正需要的信息。"""

    def test_progress_names_next_gate(self) -> None:
        state = {
            "workflow_id": "test-id",
            "workflow_version": manager.WORKFLOW_VERSION,
            "schema_version": manager.SCHEMA_VERSION,
            "revision": 3,
            "source": {
                "path": "原始资料/视频资料/测试/测试.mp4",
                "kind": "video",
                "duration_ms": 10_000,
                "identity": {"sha256": "a" * 64},
            },
            "audio": {"path": "原始资料/视频资料/测试/测试_音频.m4a"},
            "execution": {
                "mode": "staged",
                "plan_confirmed_at": "2026-08-26T00:00:00+08:00",
                "plan_hash": "b" * 64,
            },
            "stage1": {
                "status": "awaiting_continue",
                "gate": {"target_chunk": 2},
                "chunks": [
                    {
                        "index": 1,
                        "core_start_ms": 0,
                        "core_end_ms": 5_000,
                        "decode_start_ms": 0,
                        "decode_end_ms": 6_000,
                        "status": "complete",
                        "attempts": 1,
                        "segment_count": 2,
                    },
                    {
                        "index": 2,
                        "core_start_ms": 5_000,
                        "core_end_ms": 10_000,
                        "decode_start_ms": 4_000,
                        "decode_end_ms": 10_000,
                        "status": "pending",
                        "attempts": 0,
                    },
                ],
            },
            "stage2": {"status": "pending"},
            "errors": [],
        }
        markdown = manager.render_progress(state)
        self.assertIn("等待用户确认是否继续子阶段 2/2", markdown)
        self.assertIn("不要重新提取完整音频", markdown)
        self.assertIn("001", markdown)


class MockedLifecycleTests(unittest.TestCase):
    """用一秒合成音频和模拟识别结果验证完整第一阶段，不加载大模型。"""

    def create_database(self, temporary: str) -> tuple[Path, Path]:
        root = Path(temporary) / manager.ROOT_NAME
        toolkit = root / manager.TOOLKIT_RELATIVE
        task = root / "原始资料" / "视频资料" / "模拟任务"
        toolkit.mkdir(parents=True)
        task.mkdir(parents=True)
        (root / manager.DATABASE_MARKER).write_text("{}", encoding="utf-8")
        config = {
            "audio_bitrate": "128k",
            "whisper_model": "工具/资料整理工具/runtime/models/model",
            "terminology_file": "工具/资料整理工具/dictionaries/terminology.json",
            "cooked_dir": "Markdown",
        }
        (toolkit / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        audio = task / "模拟任务.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16_000)
            handle.writeframes(b"\x00\x00" * 16_000)
        return root, audio

    @staticmethod
    def fake_environment(root: Path) -> dict:
        return {
            "model": "工具/资料整理工具/runtime/models/model",
            "device": "cpu",
            "compute_type": "int8",
            "cpu_threads": 2,
            "beam_size": 3,
            "language": "zh",
            "language_probability": 1.0,
        }

    def fake_transcription(self, chunk_audio, media, chunk, config, database_root, **kwargs):
        start = int(chunk["core_start_ms"]) + 10
        end = min(int(chunk["core_end_ms"]), start + 200)
        segment = {
            "id": 1,
            "start": start / 1000,
            "end": end / 1000,
            "start_ms": start,
            "end_ms": end,
            "text": f"模拟子阶段 {chunk['index']}",
            "avg_logprob": -0.1,
            "no_speech_prob": 0.0,
            "compression_ratio": 1.0,
            "chunk_index": int(chunk["index"]),
        }
        return {"segments": [segment], "metrics": {"segment_count": 1, "decoded_duration_ms": 500, "duration_after_vad_ms": 200}}, self.fake_environment(database_root)

    @staticmethod
    def fake_extract(audio: Path, chunk: dict, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"mock-wave")

    def test_one_chunk_stage1_commits_complete_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, audio = self.create_database(temporary)
            state = manager.initialize_state(
                audio,
                root,
                mode="staged",
                chunk_count=1,
                overlap_seconds=0,
                force_audio=False,
                requested_device="cpu",
                requested_compute="int8",
            )
            with (
                mock.patch.object(manager, "extract_chunk_audio", side_effect=self.fake_extract),
                mock.patch.object(manager, "transcribe_chunk", side_effect=self.fake_transcription),
            ):
                manager.run_next_chunk(
                    state,
                    audio,
                    root,
                    requested_index=1,
                    device_arg="cpu",
                    compute_arg="int8",
                )
            self.assertEqual(state["stage1"]["status"], "complete")
            task = audio.parent
            for suffix in (".md", ".srt", ".json", ".jsonl"):
                self.assertTrue((task / "01_原始逐字稿").with_suffix(suffix).is_file())
            self.assertTrue((task / manager.FINAL_BUNDLE_MANIFEST).is_file())
            self.assertTrue((task / manager.HANDOFF_DIRNAME / "第一阶段完成.md").is_file())
            manager.verify_stage1_outputs(state, root)

    def test_staged_mode_forces_continue_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, audio = self.create_database(temporary)
            state = manager.initialize_state(
                audio,
                root,
                mode="staged",
                chunk_count=2,
                overlap_seconds=0,
                force_audio=False,
                requested_device="cpu",
                requested_compute="int8",
            )
            with (
                mock.patch.object(manager, "extract_chunk_audio", side_effect=self.fake_extract),
                mock.patch.object(manager, "transcribe_chunk", side_effect=self.fake_transcription),
            ):
                manager.run_next_chunk(
                    state,
                    audio,
                    root,
                    requested_index=1,
                    device_arg="cpu",
                    compute_arg="int8",
                )
                self.assertEqual(state["stage1"]["status"], "awaiting_continue")
                with self.assertRaises(manager.WorkflowError):
                    manager.run_next_chunk(
                        state,
                        audio,
                        root,
                        requested_index=2,
                        device_arg="cpu",
                        compute_arg="int8",
                    )
            self.assertEqual(manager.next_action(state), "等待用户确认是否继续子阶段 2/2")

    def test_two_chunk_lifecycle_uses_previous_text_merges_and_compacts(self) -> None:
        """末段使用前文，正式总稿保留，分段目录收口且不会被第二阶段复活。"""

        with tempfile.TemporaryDirectory() as temporary:
            root, audio = self.create_database(temporary)
            state = manager.initialize_state(
                audio,
                root,
                mode="staged",
                chunk_count=2,
                overlap_seconds=0,
                force_audio=False,
                requested_device="cpu",
                requested_compute="int8",
            )
            received_contexts = []

            def capture_transcription(*args, **kwargs):
                received_contexts.append(kwargs.get("previous_context_text", ""))
                return self.fake_transcription(*args, **kwargs)

            with (
                mock.patch.object(manager, "extract_chunk_audio", side_effect=self.fake_extract),
                mock.patch.object(manager, "transcribe_chunk", side_effect=capture_transcription),
            ):
                manager.run_next_chunk(
                    state, audio, root, requested_index=1, device_arg="cpu", compute_arg="int8"
                )
                manager.command_approve_next(
                    SimpleNamespace(input=audio, database_root=root, confirmed_by_user=True)
                )
                state = manager.load_state(audio)
                manager.run_next_chunk(
                    state, audio, root, requested_index=2, device_arg="cpu", compute_arg="int8"
                )

            self.assertEqual(received_contexts[0], "")
            self.assertIn("模拟子阶段 1", received_contexts[1])
            self.assertEqual(state["stage1"]["status"], "complete")
            self.assertEqual(state["stage1"]["intermediate_cleanup"]["status"], "complete")
            self.assertFalse((audio.parent / manager.PARTS_DIRNAME).exists())
            self.assertTrue((audio.parent / manager.HANDOFF_DIRNAME).is_dir())
            total = json.loads(
                (audio.parent / "01_原始逐字稿.json").read_text(encoding="utf-8-sig")
            )
            self.assertEqual([item["text"] for item in total["segments"]], ["模拟子阶段 1", "模拟子阶段 2"])
            commit = json.loads(
                (audio.parent / manager.FINAL_BUNDLE_MANIFEST).read_text(encoding="utf-8-sig")
            )
            self.assertEqual(commit["chunks"][1]["previous_chunk_context"]["source_chunk"], 1)
            self.assertEqual(len(commit["chunks"]), 2)
            completion = (
                audio.parent / manager.HANDOFF_DIRNAME / "第一阶段完成.md"
            ).read_text(encoding="utf-8-sig")
            self.assertIn("永久保留的第一阶段成果", completion)
            self.assertIn("已删除", completion)
            self.assertIn(manager.PARTS_DIRNAME, completion)

            manager.command_stage2_start(
                SimpleNamespace(input=audio, database_root=root, confirmed_by_user=True)
            )
            self.assertFalse((audio.parent / manager.PARTS_DIRNAME).exists())

    def test_empty_formal_output_records_cannot_authorize_compaction(self) -> None:
        """merge.outputs 为空时不得因循环零次而误判正式总稿有效。"""

        with tempfile.TemporaryDirectory() as temporary:
            root, audio = self.create_database(temporary)
            state = manager.initialize_state(
                audio,
                root,
                mode="staged",
                chunk_count=1,
                overlap_seconds=0,
                force_audio=False,
                requested_device="cpu",
                requested_compute="int8",
            )
            with (
                mock.patch.object(manager, "extract_chunk_audio", side_effect=self.fake_extract),
                mock.patch.object(manager, "transcribe_chunk", side_effect=self.fake_transcription),
            ):
                manager.run_next_chunk(
                    state, audio, root, requested_index=1, device_arg="cpu", compute_arg="int8"
                )
            state["stage1"]["merge"]["outputs"] = []
            with self.assertRaisesRegex(manager.WorkflowError, "必须完整登记"):
                manager.verify_stage1_outputs(state, root)

    def test_unknown_part_blocks_only_compaction_and_retry_does_not_remerge(self) -> None:
        """未知分段文件不破坏正式 01；移走后只重试收口。"""

        with tempfile.TemporaryDirectory() as temporary:
            root, audio = self.create_database(temporary)
            state = manager.initialize_state(
                audio,
                root,
                mode="staged",
                chunk_count=1,
                overlap_seconds=0,
                force_audio=False,
                requested_device="cpu",
                requested_compute="int8",
            )
            unknown = audio.parent / manager.PARTS_DIRNAME / "人工保留.txt"
            unknown.write_text("先移走", encoding="utf-8")
            with (
                mock.patch.object(manager, "extract_chunk_audio", side_effect=self.fake_extract),
                mock.patch.object(manager, "transcribe_chunk", side_effect=self.fake_transcription),
            ):
                manager.run_next_chunk(
                    state, audio, root, requested_index=1, device_arg="cpu", compute_arg="int8"
                )
            self.assertEqual(state["stage1"]["status"], "complete")
            self.assertEqual(state["stage1"]["merge"]["status"], "complete")
            self.assertEqual(
                state["stage1"]["intermediate_cleanup"]["status"], "blocked_unknown_files"
            )
            self.assertTrue(unknown.is_file())
            manager.verify_stage1_outputs(state, root)

            unknown.unlink()
            with mock.patch.object(manager, "merge_segments") as forbidden_remerge:
                manager.command_compact_stage1(
                    SimpleNamespace(input=audio, database_root=root, confirmed_by_user=True)
                )
                forbidden_remerge.assert_not_called()
            reloaded = manager.load_state(audio)
            self.assertEqual(reloaded["stage1"]["intermediate_cleanup"]["status"], "complete")
            self.assertFalse((audio.parent / manager.PARTS_DIRNAME).exists())

    def test_handoff_note_does_not_block_stage1_compaction(self) -> None:
        """第一阶段只清理分段目录，交接目录中的人工文件原样保留。"""

        with tempfile.TemporaryDirectory() as temporary:
            root, audio = self.create_database(temporary)
            state = manager.initialize_state(
                audio,
                root,
                mode="staged",
                chunk_count=1,
                overlap_seconds=0,
                force_audio=False,
                requested_device="cpu",
                requested_compute="int8",
            )
            note = audio.parent / manager.HANDOFF_DIRNAME / "人工交接补充.md"
            note.write_text("必须保留", encoding="utf-8")
            with (
                mock.patch.object(manager, "extract_chunk_audio", side_effect=self.fake_extract),
                mock.patch.object(manager, "transcribe_chunk", side_effect=self.fake_transcription),
            ):
                manager.run_next_chunk(
                    state, audio, root, requested_index=1, device_arg="cpu", compute_arg="int8"
                )
            self.assertEqual(state["stage1"]["intermediate_cleanup"]["status"], "complete")
            self.assertFalse((audio.parent / manager.PARTS_DIRNAME).exists())
            self.assertEqual(note.read_text(encoding="utf-8"), "必须保留")

    def test_modified_previous_json_blocks_next_transcription(self) -> None:
        """上一段逐字稿被改动时，后一段不能静默退回无前文。"""

        with tempfile.TemporaryDirectory() as temporary:
            root, audio = self.create_database(temporary)
            state = manager.initialize_state(
                audio,
                root,
                mode="staged",
                chunk_count=2,
                overlap_seconds=0,
                force_audio=False,
                requested_device="cpu",
                requested_compute="int8",
            )
            with (
                mock.patch.object(manager, "extract_chunk_audio", side_effect=self.fake_extract),
                mock.patch.object(manager, "transcribe_chunk", side_effect=self.fake_transcription),
            ):
                manager.run_next_chunk(
                    state, audio, root, requested_index=1, device_arg="cpu", compute_arg="int8"
                )
            manager.command_approve_next(
                SimpleNamespace(input=audio, database_root=root, confirmed_by_user=True)
            )
            state = manager.load_state(audio)
            part_json = audio.parent / manager.PARTS_DIRNAME / "part-001.json"
            part_json.write_text(part_json.read_text(encoding="utf-8-sig") + " ", encoding="utf-8")
            with (
                mock.patch.object(manager, "extract_chunk_audio", side_effect=self.fake_extract),
                mock.patch.object(manager, "transcribe_chunk") as forbidden_transcription,
                self.assertRaisesRegex(manager.WorkflowError, "大小改变|哈希不符"),
            ):
                manager.run_next_chunk(
                    state, audio, root, requested_index=2, device_arg="cpu", compute_arg="int8"
                )
            forbidden_transcription.assert_not_called()

    def test_nonfinal_commit_failure_does_not_enter_merge_state(self) -> None:
        """非末段交接提交失败时应只重试该段，不能误进 ready_to_merge。"""

        with tempfile.TemporaryDirectory() as temporary:
            root, audio = self.create_database(temporary)
            state = manager.initialize_state(
                audio,
                root,
                mode="staged",
                chunk_count=2,
                overlap_seconds=0,
                force_audio=False,
                requested_device="cpu",
                requested_compute="int8",
            )
            original_persist = manager.persist_state
            calls = 0

            def fail_second_persist(current_state, current_media, current_root):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise manager.WorkflowError("模拟交接提交失败")
                return original_persist(current_state, current_media, current_root)

            with (
                mock.patch.object(manager, "extract_chunk_audio", side_effect=self.fake_extract),
                mock.patch.object(manager, "transcribe_chunk", side_effect=self.fake_transcription),
                mock.patch.object(manager, "persist_state", side_effect=fail_second_persist),
            ):
                with self.assertRaisesRegex(manager.WorkflowError, "模拟交接提交失败"):
                    manager.run_next_chunk(
                        state,
                        audio,
                        root,
                        requested_index=1,
                        device_arg="cpu",
                        compute_arg="int8",
                    )
            self.assertEqual(state["stage1"]["status"], "failed_recoverable")
            self.assertEqual(state["stage1"]["current_chunk"], 1)
            self.assertEqual(state["stage1"]["chunks"][0]["status"], "failed_recoverable")
            self.assertEqual(state["stage1"]["chunks"][1]["status"], "pending")
            self.assertEqual(state["stage1"]["merge"]["status"], "pending")
            self.assertIsNone(state["stage1"]["gate"])
            self.assertEqual(state["errors"][-1]["operation"], "transcribe_chunk")
            reloaded = manager.load_state(audio)
            self.assertIsNone(reloaded["stage1"]["gate"])
            existing, issue = manager.inspect_existing_workflow(
                audio,
                root,
                audio.parent / manager.HANDOFF_DIRNAME / manager.STATE_FILENAME,
            )
            self.assertIsNone(issue)
            self.assertEqual(existing["stage1_status"], "failed_recoverable")

    def test_merge_failure_keeps_final_chunk_complete(self) -> None:
        """末段转写已提交时，合并失败只能重做 merge，不能重转末段。"""

        with tempfile.TemporaryDirectory() as temporary:
            root, audio = self.create_database(temporary)
            state = manager.initialize_state(
                audio,
                root,
                mode="staged",
                chunk_count=1,
                overlap_seconds=0,
                force_audio=False,
                requested_device="cpu",
                requested_compute="int8",
            )
            with (
                mock.patch.object(manager, "extract_chunk_audio", side_effect=self.fake_extract),
                mock.patch.object(manager, "transcribe_chunk", side_effect=self.fake_transcription),
                mock.patch.object(manager, "merge_stage1", side_effect=manager.WorkflowError("模拟合并失败")),
            ):
                with self.assertRaisesRegex(manager.WorkflowError, "模拟合并失败"):
                    manager.run_next_chunk(
                        state,
                        audio,
                        root,
                        requested_index=1,
                        device_arg="cpu",
                        compute_arg="int8",
                    )
            self.assertEqual(state["stage1"]["chunks"][0]["status"], "complete")
            self.assertEqual(state["stage1"]["status"], "ready_to_merge")
            self.assertIsNone(state["stage1"]["current_chunk"])
            self.assertEqual(state["stage1"]["merge"]["status"], "failed_recoverable")
            self.assertEqual(state["errors"][-1]["operation"], "merge")
            reloaded = manager.load_state(audio)
            self.assertEqual(reloaded["stage1"]["chunks"][0]["status"], "complete")
            self.assertEqual(manager.next_action(reloaded), "校验全部子阶段并合并四种 01_原始逐字稿")

    def test_complete_can_retry_cleanup_after_unknown_file_is_moved(self) -> None:
        """成果已验证但清理被阻止后，移走未知文件即可重试 complete。"""

        with tempfile.TemporaryDirectory() as temporary:
            root, audio = self.create_database(temporary)
            state = manager.initialize_state(
                audio,
                root,
                mode="staged",
                chunk_count=1,
                overlap_seconds=0,
                force_audio=False,
                requested_device="cpu",
                requested_compute="int8",
            )
            with (
                mock.patch.object(manager, "extract_chunk_audio", side_effect=self.fake_extract),
                mock.patch.object(manager, "transcribe_chunk", side_effect=self.fake_transcription),
            ):
                manager.run_next_chunk(
                    state,
                    audio,
                    root,
                    requested_index=1,
                    device_arg="cpu",
                    compute_arg="int8",
                )
            manager.command_stage2_start(
                SimpleNamespace(input=audio, database_root=root, confirmed_by_user=True)
            )
            corrected = audio.parent / "02_校正逐字稿.md"
            final = audio.with_suffix(".md")
            cooked = root / "Markdown" / final.name
            corrected.write_text("校正稿记录了一个需要完整保留的具体主张。", encoding="utf-8")
            final.write_text("最终研究记录完整保留了这个具体主张。", encoding="utf-8")
            write_valid_coverage_report(corrected, final)
            cooked.parent.mkdir(parents=True)
            cooked.write_text("最终研究记录完整保留了这个具体主张。", encoding="utf-8")
            unknown = audio.parent / manager.HANDOFF_DIRNAME / "人工备注.md"
            unknown.write_text("应移到参考资料", encoding="utf-8")
            args = SimpleNamespace(
                input=audio,
                database_root=root,
                corrected=None,
                final=None,
                cooked=None,
                ingest_confirmed=False,
            )
            with self.assertRaisesRegex(manager.WorkflowError, "尚未确认自动备份"):
                manager.command_complete(args)
            args.ingest_confirmed = True
            with self.assertRaisesRegex(manager.WorkflowError, "临时目录含未登记文件"):
                manager.command_complete(args)
            blocked = manager.load_state(audio)
            self.assertEqual(blocked["stage2"]["status"], "complete")
            self.assertEqual(blocked["stage2"]["cleanup_status"], "blocked_unknown_files")
            self.assertEqual(blocked["stage2"]["ingestion"]["status"], "verified")

            unknown.unlink()
            manager.command_complete(args)
            self.assertFalse((audio.parent / manager.HANDOFF_DIRNAME).exists())
            self.assertFalse((audio.parent / manager.PARTS_DIRNAME).exists())
            for path in (audio, corrected, final, cooked, audio.parent / "01_原始逐字稿.md"):
                self.assertTrue(path.is_file())

    def test_complete_validates_visual_docx_only_when_explicitly_selected(self) -> None:
        """--cooked-visual 出现时验证 DOCX 双份哈希。"""

        with tempfile.TemporaryDirectory() as temporary:
            root, audio = self.create_database(temporary)
            state = manager.initialize_state(
                audio,
                root,
                mode="staged",
                chunk_count=1,
                overlap_seconds=0,
                force_audio=False,
                requested_device="cpu",
                requested_compute="int8",
            )
            with (
                mock.patch.object(manager, "extract_chunk_audio", side_effect=self.fake_extract),
                mock.patch.object(manager, "transcribe_chunk", side_effect=self.fake_transcription),
            ):
                manager.run_next_chunk(
                    state, audio, root, requested_index=1, device_arg="cpu", compute_arg="int8"
                )
            manager.command_stage2_start(
                SimpleNamespace(input=audio, database_root=root, confirmed_by_user=True)
            )
            corrected = audio.parent / "02_校正逐字稿.md"
            final = audio.with_suffix(".md")
            cooked = root / "Markdown" / final.name
            visual = audio.parent / "03_视觉资料.docx"
            cooked_visual = root / "Markdown" / visual.name
            corrected.write_text("校正稿记录了一个需要完整保留的具体主张。", encoding="utf-8")
            final.write_text("最终研究记录完整保留了这个具体主张。", encoding="utf-8")
            write_valid_coverage_report(corrected, final)
            cooked.parent.mkdir(parents=True)
            cooked.write_text("最终研究记录完整保留了这个具体主张。", encoding="utf-8")
            visual.write_bytes(b"docx-source")
            cooked_visual.write_bytes(b"docx-mismatch")
            args = SimpleNamespace(
                input=audio,
                database_root=root,
                corrected=None,
                final=None,
                cooked=None,
                ingest_confirmed=True,
                visual=visual,
                cooked_visual=cooked_visual,
            )
            with self.assertRaisesRegex(manager.WorkflowError, "DOCX 副本 SHA256 不一致"):
                manager.command_complete(args)
            self.assertTrue((audio.parent / manager.HANDOFF_DIRNAME).is_dir())

            cooked_visual.write_bytes(b"docx-source")
            manager.command_complete(args)
            self.assertFalse((audio.parent / manager.HANDOFF_DIRNAME).exists())
            self.assertTrue(visual.is_file())
            self.assertTrue(cooked_visual.is_file())

    def test_complete_keeps_visual_local_when_word_backup_is_not_authorized(self) -> None:
        """最终 Markdown 强制备份不能连带强制复制 Word 视觉附件。"""

        with tempfile.TemporaryDirectory() as temporary:
            root, audio = self.create_database(temporary)
            state = manager.initialize_state(
                audio,
                root,
                mode="staged",
                chunk_count=1,
                overlap_seconds=0,
                force_audio=False,
                requested_device="cpu",
                requested_compute="int8",
            )
            with (
                mock.patch.object(manager, "extract_chunk_audio", side_effect=self.fake_extract),
                mock.patch.object(manager, "transcribe_chunk", side_effect=self.fake_transcription),
            ):
                manager.run_next_chunk(
                    state, audio, root, requested_index=1, device_arg="cpu", compute_arg="int8"
                )
            manager.command_stage2_start(
                SimpleNamespace(input=audio, database_root=root, confirmed_by_user=True)
            )
            corrected = audio.parent / "02_校正逐字稿.md"
            final = audio.with_suffix(".md")
            cooked = root / "Markdown" / final.name
            visual = audio.parent / "03_视觉资料.docx"
            corrected.write_text("校正稿记录了一个需要完整保留的具体主张。", encoding="utf-8")
            final.write_text("最终研究记录完整保留了这个具体主张。", encoding="utf-8")
            write_valid_coverage_report(corrected, final)
            cooked.parent.mkdir(parents=True)
            cooked.write_text("最终研究记录完整保留了这个具体主张。", encoding="utf-8")
            visual.write_bytes(b"docx-source")

            manager.command_complete(
                SimpleNamespace(
                    input=audio,
                    database_root=root,
                    corrected=None,
                    final=None,
                    cooked=None,
                    ingest_confirmed=True,
                    visual=visual,
                    cooked_visual=None,
                )
            )

            self.assertTrue(visual.is_file())
            self.assertFalse((root / "Markdown" / visual.name).exists())


if __name__ == "__main__":
    unittest.main()
