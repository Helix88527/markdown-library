"""Behavior of text organization and flat Markdown with source evidence."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import assemble_record
import archive_bundle
import flat_archive
import media_stage_manager as manager
from project_io import ProjectError
import test_media_stage_manager as lifecycle


class TextOrganizationTests(unittest.TestCase):
    def test_existing_text_can_assemble_without_correction_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            source, analysis, final, report = (p / name for name in ('01.md', 'analysis.md', 'final.md', 'report.json'))
            source.write_text('如果条件成立，结论才适用。\n\n例子 A 和例子 B。\n', encoding='utf-8')
            sections = [h for h in assemble_record.REQUIRED_SECTIONS if h != '## 重要校正与存疑项']
            analysis.write_text('\n\n'.join(h + '\n\n说明' for h in sections), encoding='utf-8')
            metadata = {'title': '文章', 'material_type': 'article_html', 'authorship': 'repost', 'source_files': ['article.html']}
            assemble_record.assemble(source, analysis, metadata, final, report)
            text = final.read_text(encoding='utf-8')
            self.assertIn('## 正文整理', text)
            self.assertNotIn('## 重要校正与存疑项', text)
            self.assertIn(source.read_text(encoding='utf-8'), text)
            self.assertEqual(assemble_record.verify(source, final, report)['status'], 'passed')
            metadata['material_type'] = 'audio'
            with self.assertRaises(ProjectError):
                assemble_record.assemble(source, analysis, metadata, p / 'media.md', p / 'media.json')


class FlatArchiveTests(unittest.TestCase):
    def test_no_link_bytes_and_line_endings_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / 'a.md'
            content = b'\xef\xbb\xbf# Title\r\n\r\nOriginal text.\r\n'
            entry.write_bytes(content)
            result = flat_archive.archive(entry, root / 'cooked', database_root=root)
            self.assertEqual(Path(result['destination']).read_bytes(), content)

    def test_flat_archive_rebases_all_link_forms_and_keeps_evidence_at_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / '原始资料' / '一篇 文章'
            cooked = root / '成稿'
            refs = source / '参考'
            refs.mkdir(parents=True)
            (source / '图 (1).png').write_bytes(b'picture')
            (refs / '证据.md').write_text('[图片](<../图 (1).png>)', encoding='utf-8')
            entry = source / '文章.md'
            entry.write_text(
                '# 正文\n\n[材料](参考/证据.md?q=1#段落)\n\n'
                '![图](<图 (1).png> "标题")\n\n[ref]: <参考/证据.md#段落>\n'
                '<a href="参考/证据.md">参考</a>\n\n[网页](https://example.org/p#x)\n'
                '[本页](#正文)\n\n```md\n[x](missing.md)\n```\n'
                '`[x](missing2.md)`\n', encoding='utf-8')
            before = entry.read_bytes()
            result = flat_archive.archive(entry, cooked, database_root=root)
            final = Path(result['destination'])
            self.assertEqual(final.parent, cooked)
            self.assertEqual(list(cooked.iterdir()), [final])
            self.assertEqual(result['dependency_count'], 2)
            self.assertEqual(entry.read_bytes(), before)
            output = unquote(final.read_text(encoding='utf-8'))
            self.assertIn('../原始资料/一篇 文章/参考/证据.md?q=1#段落', output)
            self.assertIn('![图](<../原始资料/一篇 文章/图 (1).png> "标题")', output)
            self.assertIn('[ref]: <../原始资料/一篇 文章/参考/证据.md#段落>', output)
            self.assertIn('<a href="../原始资料/一篇 文章/参考/证据.md">', output)
            self.assertIn('[网页](https://example.org/p#x)', output)
            self.assertIn('[x](missing.md)', output)
            self.assertEqual(flat_archive.archive(entry, cooked, database_root=root)['status'], 'unchanged')
            (refs / '证据.md').unlink()
            with self.assertRaises(ProjectError):
                flat_archive.verify_flat(entry, final, database_root=root)

    def test_missing_escape_conflict_and_racing_writer_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'database'
            source = root / 'raw'
            source.mkdir(parents=True)
            entry = source / 'title.md'
            cooked = root / 'cooked'
            for body in ('[bad](missing.md)', '[bad](../../../outside.md)'):
                entry.write_text(body, encoding='utf-8')
                with self.assertRaises(ProjectError):
                    flat_archive.archive(entry, cooked, database_root=root)
                self.assertFalse(cooked.exists())
            entry.write_text('原文', encoding='utf-8')
            result = flat_archive.archive(entry, cooked, database_root=root)
            final = Path(result['destination'])
            final.write_text('人工修改', encoding='utf-8')
            with self.assertRaises(ProjectError):
                flat_archive.archive(entry, cooked, database_root=root)
            self.assertEqual(final.read_text(encoding='utf-8'), '人工修改')
            with mock.patch.object(flat_archive.os, 'link', side_effect=FileExistsError):
                with self.assertRaises(ProjectError):
                    flat_archive.archive(entry, cooked, database_root=root, archive_name='修订')
            self.assertFalse(list(cooked.glob('.mdlib-flat-*')))

    def test_external_project_and_nested_cycle_remain_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, source = base / 'db', base / 'external'
            root.mkdir()
            source.mkdir()
            entry = source / 'a.md'
            entry.write_text('[b](b.md)', encoding='utf-8')
            (source / 'b.md').write_text('[a](a.md)', encoding='utf-8')
            result = flat_archive.archive(entry, root / 'cooked', database_root=root)
            self.assertEqual(result['dependency_count'], 1)
            self.assertTrue((source / 'b.md').is_file())
            self.assertEqual(flat_archive.verify_flat(entry, Path(result['destination']), database_root=root)['status'], 'verified')


class FlatMediaCompletionTests(unittest.TestCase):
    def prepare(self, temporary, attachment):
        fixture = lifecycle.MockedLifecycleTests()
        root, audio = fixture.create_database(temporary)
        state = manager.initialize_state(audio, root, mode='staged', chunk_count=1, overlap_seconds=0,
                                         force_audio=False, requested_device='cpu', requested_compute='int8')
        with mock.patch.object(manager, 'extract_chunk_audio', side_effect=fixture.fake_extract), mock.patch.object(manager, 'transcribe_chunk', side_effect=fixture.fake_transcription):
            manager.run_next_chunk(state, audio, root, requested_index=1, device_arg='cpu', compute_arg='int8')
        manager.command_stage2_start(SimpleNamespace(input=audio, database_root=root, confirmed_by_user=True))
        corrected, final = audio.parent / '02_校正逐字稿.md', audio.with_suffix('.md')
        evidence = audio.parent / attachment
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text('持久证据', encoding='utf-8')
        corrected.write_text('主张及条件。\n\n[证据](<' + attachment + '>)\n', encoding='utf-8')
        analysis = audio.parent / 'analysis.md'
        analysis.write_text('\n\n'.join(h + '\n\n说明' for h in assemble_record.REQUIRED_SECTIONS), encoding='utf-8')
        report = audio.parent / manager.HANDOFF_DIRNAME / manager.COVERAGE_REPORT_FILENAME
        assemble_record.assemble(corrected, analysis, {'title':'媒体', 'material_type':'audio','authorship':'original','source_files':[audio.name]}, final, report)
        args = SimpleNamespace(input=audio, database_root=root, corrected=None, final=None, cooked=None, ingest_confirmed=True)
        return root, audio, final, evidence, args

    def test_complete_accepts_rebased_content_and_preserves_source_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, audio, final, evidence, args = self.prepare(tmp, '参考资料/证据.txt')
            result = flat_archive.archive(final, root / 'Markdown', database_root=root)
            self.assertNotEqual(final.read_bytes(), Path(result['destination']).read_bytes())
            manager.command_complete(args)
            self.assertTrue(evidence.is_file())
            self.assertFalse((audio.parent / manager.HANDOFF_DIRNAME).exists())

    def test_complete_rejects_link_to_cleanup_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, audio, final, evidence, args = self.prepare(tmp, manager.HANDOFF_DIRNAME + '/证据.txt')
            flat_archive.archive(final, root / 'Markdown', database_root=root)
            with self.assertRaisesRegex(manager.WorkflowError, '待清理'):
                manager.command_complete(args)
            self.assertTrue(evidence.is_file())
            self.assertTrue((audio.parent / manager.HANDOFF_DIRNAME).exists())

    def test_old_bundle_still_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, audio, final, evidence, args = self.prepare(tmp, '参考资料/证据.txt')
            result = archive_bundle.archive(final, root / 'Markdown')
            manager.command_complete(args)
            self.assertTrue(Path(result['destination']).is_file())


if __name__ == '__main__':
    unittest.main()
