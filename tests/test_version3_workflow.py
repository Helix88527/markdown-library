"""Behavioral tests of input identity, full text, portable archives and GPU gates."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import archive_bundle
import assemble_record
import audit_source_fidelity
import extract_article
import extract_x_html
import inspect_project
import media_stage_manager
import reference_library
import search_cooked_archive
from project_io import ProjectError, dependency_files


def card(identifier='123', text='原作者的判断', extra='', account='author'):
    return f'<article data-testid="tweet"><div data-testid="User-Name">@{account}</div><a href="/{account}/status/{identifier}"><time datetime="2026-09-01T12:00:00Z">Sep 1</time></a><div data-testid="tweetText">{text}</div>{extra}</article>'


class XExtractionTests(unittest.TestCase):
    def test_image_only_quote_is_owned_by_quoted_author(self):
        quote = '<div role="link"><div data-testid="User-Name">@other</div><a href="/other/status/456"><time>date</time></a><div data-testid="tweetPhoto"><img src="quoted.png"></div></div>'
        post = extract_x_html.extract_posts(card(extra=quote))['posts'][0]
        self.assertEqual(post['relation'], 'quote')
        self.assertEqual(post['media'], [])
        self.assertEqual(post['quoted_posts'][0]['author_account'], 'other')
        self.assertEqual(len(post['quoted_posts'][0]['media']), 1)

    def test_quote_and_author_are_separate(self):
        quoted = '<div role="link"><div data-testid="User-Name">@other</div><a href="/other/status/456"><time datetime="2026-08-31">旧帖</time></a><div data-testid="tweetText">被引用者的主张</div></div>'
        result = extract_x_html.extract_posts(card(extra=quoted))
        post = result['posts'][0]
        self.assertEqual(post['text'], '原作者的判断')
        self.assertEqual(post['author_account'], 'author')
        self.assertEqual(post['quoted_posts'][0]['author_account'], 'other')
        self.assertEqual(post['quoted_posts'][0]['text'], '被引用者的主张')
        self.assertEqual(post['relation'], 'quote')

    def test_duplicates_and_missing_parent(self):
        html = card().replace('<article ', '<article data-in-reply-to-status-id="999" ')
        result = extract_x_html.extract_posts(html + html)
        self.assertEqual(len(result['posts']), 1)
        self.assertTrue(any('999' in w for w in result['warnings']))

    def test_empty_shell_fails_and_no_identity_guessed_from_mentioned_link(self):
        with self.assertRaises(ProjectError):
            extract_x_html.extract_posts('<html><title>X</title></html>')
        html = '<article data-testid="tweet"><div data-testid="tweetText">参考 <a href="/somebody/status/444">这条推文</a></div></article>'
        post = extract_x_html.extract_posts(html)['posts'][0]
        self.assertIsNone(post['post_id'])
        self.assertIsNone(post['author_account'])

    def test_article_entrypoint_routes_x_and_reuses_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'x.html'
            path.write_text(card(), encoding='utf-8')
            result = extract_article.extract_file(path)
            self.assertEqual(result['post_count'], 1)
            self.assertEqual(extract_article.extract_file(path)['status'], 'reused')
            body = Path(result['result_directory']) / '01_原始正文.md'
            body.write_text('人工修改', encoding='utf-8')
            with self.assertRaises(extract_article.ExtractionError):
                extract_article.extract_file(path)


class FullTextAndArchiveTests(unittest.TestCase):
    def fixture(self, root):
        project = root / 'source'
        project.mkdir()
        corrected = project / '02.md'
        corrected.write_text('甲在 1992 年提出问题。\n\n问：为什么？\n答：暂不确定。\n', encoding='utf-8')
        analysis = project / 'analysis.md'
        analysis.write_text('\n\n'.join(h + '\n\n已检查；本项不适用或见说明。' for h in assemble_record.REQUIRED_SECTIONS), encoding='utf-8')
        output = project / '记录.md'
        report = project / 'coverage.json'
        metadata = {'title': '记录', 'material_type': 'audio', 'authorship': 'original', 'source_files': ['input.mp3'], 'processed_at': '2026-09-07'}
        assemble_record.assemble(corrected, analysis, metadata, output, report)
        return project, corrected, output, report

    def test_exact_fulltext_gate_and_changed_source_or_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, corrected, final, report = self.fixture(Path(tmp))
            self.assertEqual(audit_source_fidelity.audit_report(corrected, final, report)['status'], 'passed')
            final.write_text(final.read_text(encoding='utf-8').replace('1992', '1993'), encoding='utf-8')
            with self.assertRaises(audit_source_fidelity.FidelityError):
                audit_source_fidelity.audit_report(corrected, final, report)

    def test_archive_copies_nested_references_and_preserves_manual_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, corrected, final, report = self.fixture(root)
            image = project / 'assets' / '图.png'
            image.parent.mkdir()
            image.write_bytes(b'image')
            evidence = project / '参考资料' / '依据.md'
            evidence.parent.mkdir()
            evidence.write_text('# 依据\n\n![图](../assets/图.png)', encoding='utf-8')
            with final.open('a', encoding='utf-8') as f:
                f.write('\n[依据](参考资料/依据.md)\n')
            cooked = root / 'cooked'
            result = archive_bundle.archive(final, cooked)
            self.assertEqual(result['file_count'], 3)
            self.assertTrue((cooked / '记录' / 'assets' / '图.png').exists())
            self.assertEqual(archive_bundle.archive(final, cooked)['status'], 'unchanged')
            (cooked / '记录' / 'assets' / '图.png').write_bytes(b'edited')
            with self.assertRaises(ProjectError):
                archive_bundle.archive(final, cooked)
            self.assertEqual((cooked / '记录' / 'assets' / '图.png').read_bytes(), b'edited')

    def test_missing_or_escaping_link_prevents_any_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, _, final, _ = self.fixture(root)
            for link in ('missing.png', '../outside.txt'):
                final.write_text(f'![图]({link})', encoding='utf-8')
                with self.assertRaises(ProjectError):
                    archive_bundle.archive(final, root / 'cooked')
                self.assertFalse((root / 'cooked').exists())

    def test_code_examples_are_not_dependencies_and_reference_links_are(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'file name.pdf').write_bytes(b'pdf')
            entry = root / 'a.md'
            entry.write_text('```md\n![example](missing.png)\n```\n\n[ref][r]\n\n[r]: <file name.pdf>\n', encoding='utf-8')
            self.assertEqual(len(dependency_files(entry)), 2)
            (root / 'report(1).pdf').write_bytes(b'pdf')
            entry.write_text('[附件](report(1).pdf)', encoding='utf-8')
            self.assertEqual(len(dependency_files(entry)), 2)

    def test_archive_search_excludes_reference_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            (source / 'ref.md').write_text('甲乙研究 甲乙研究 甲乙研究', encoding='utf-8')
            entry = source / '记录.md'
            entry.write_text('# 甲乙研究\n\n[参考](ref.md)', encoding='utf-8')
            cooked = root / 'cooked'
            archive_bundle.archive(entry, cooked)
            query = root / 'query.md'
            query.write_text('甲乙研究', encoding='utf-8')
            result = search_cooked_archive.search_archive(query, cooked)
            self.assertEqual(result['candidate_count'], 1)


class ReferenceAndRoutingTests(unittest.TestCase):
    def test_markdown_reference_keeps_its_image_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / 'source'
            source_dir.mkdir()
            (source_dir / 'chart.png').write_bytes(b'image')
            source = source_dir / 'ref.md'
            source.write_text('![图](chart.png)', encoding='utf-8')
            result = reference_library.register(root / 'project', source, title='依据', purpose='核查图表')
            self.assertEqual(result['file_count'], 2)
            self.assertEqual(len(dependency_files(root / 'project' / '参考资料' / '参考资料目录.md')), 3)

    def test_reference_creation_dedup_and_companion_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'project'
            project.mkdir()
            source = root / 'article.html'
            source.write_text('<img src="article_files/x.png">', encoding='utf-8')
            (root / 'article_files').mkdir()
            (root / 'article_files' / 'x.png').write_bytes(b'image')
            result = reference_library.register(project, source, title='日期依据', purpose='核对发布日期')
            self.assertEqual(result['file_count'], 2)
            self.assertEqual(reference_library.register(project, source, title='日期依据', purpose='核对发布日期')['status'], 'reused')
            self.assertEqual(len(dependency_files(project / '参考资料' / '参考资料目录.md')), 3)

    def test_absent_source_is_a_note_not_a_downloaded_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference_library.register(root, None, title='无法访问', purpose='核查事件', url='https://example.org/a', note='页面要求登录，未取得正文。')
            item = reference_library.load_registry(root)['items'][0]
            self.assertEqual(item['save_status'], '仅访问记录，未取得原件')

    def test_reference_folder_not_processed_as_primary_article(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'main.html').write_text('<article>正文</article>', encoding='utf-8')
            refs = root / '参考资料'
            refs.mkdir()
            (refs / 'other.html').write_text('参考', encoding='utf-8')
            self.assertEqual([p.name for p in extract_article.discover_sources(root)], ['main.html'])
            self.assertEqual(len(inspect_project.inspect(root)['materials']), 1)


class MediaResumeTests(unittest.TestCase):
    def test_compaction_retry_preserves_manually_edited_registered_part(self):
        from test_media_stage_manager import MockedLifecycleTests

        fixture = MockedLifecycleTests()
        manager = media_stage_manager
        with tempfile.TemporaryDirectory() as tmp:
            root, audio = fixture.create_database(tmp)
            state = manager.initialize_state(
                audio, root, mode='staged', chunk_count=1, overlap_seconds=0,
                force_audio=False, requested_device='cpu', requested_compute='int8',
            )
            parts = audio.parent / manager.PARTS_DIRNAME
            unknown = parts / '人工保留说明.md'
            unknown.write_text('收口前仍需回听核对。', encoding='utf-8')
            with mock.patch.object(manager, 'extract_chunk_audio', side_effect=fixture.fake_extract), \
                 mock.patch.object(manager, 'transcribe_chunk', side_effect=fixture.fake_transcription):
                manager.run_next_chunk(
                    state, audio, root, requested_index=1, device_arg='cpu', compute_arg='int8',
                )
            self.assertEqual(state['stage1']['intermediate_cleanup']['status'], 'blocked_unknown_files')

            edited = parts / 'part-001.md'
            original = edited.read_bytes()
            edited.write_bytes(original + '\n人工回听修订：数字为 1992。\n'.encode('utf-8'))
            corrected = audio.parent / '02_校正逐字稿.md'
            corrected.write_text('人工校正的当前全文。', encoding='utf-8')
            corrected_before = corrected.read_bytes()
            unknown.rename(audio.parent / unknown.name)
            # The machine manifest records the failed cleanup attempt and its
            # timestamp; source/ASR artifacts themselves must remain untouched.
            parts_before = {path.relative_to(parts): path.read_bytes()
                            for path in parts.rglob('*')
                            if path.is_file() and path.name != manager.MANIFEST_FILENAME}
            formal_before = {record['path']: (root / record['path']).read_bytes()
                             for record in state['stage1']['merge']['outputs']}

            with mock.patch.object(manager, 'merge_stage1') as forbidden_merge, \
                 mock.patch.object(manager, 'transcribe_chunk') as forbidden_transcription:
                manager.command_compact_stage1(
                    SimpleNamespace(input=audio, database_root=root, confirmed_by_user=True),
                )
            forbidden_merge.assert_not_called()
            forbidden_transcription.assert_not_called()
            self.assertEqual(
                {path.relative_to(parts): path.read_bytes() for path in parts.rglob('*')
                 if path.is_file() and path.name != manager.MANIFEST_FILENAME},
                parts_before,
            )
            self.assertEqual(corrected.read_bytes(), corrected_before)
            for filename, expected in formal_before.items():
                self.assertEqual((root / filename).read_bytes(), expected)
            resumed = manager.load_state(audio)
            self.assertEqual(resumed['stage1']['status'], 'complete')
            self.assertEqual(resumed['stage1']['merge']['status'], 'complete')
            self.assertNotEqual(resumed['stage1']['intermediate_cleanup']['status'], 'complete')
            self.assertTrue(resumed['stage1']['intermediate_cleanup'].get('last_error'))
            self.assertTrue((audio.parent / manager.HANDOFF_DIRNAME / manager.STATE_FILENAME).is_file())


class MediaDefaultsTests(unittest.TestCase):
    def test_short_media_defaults_continuous(self):
        args = media_stage_manager.build_parser().parse_args(['init', 'audio.mp3'])
        self.assertEqual(args.mode, 'continuous')
        self.assertFalse(args.confirmed_by_user)

    def test_auto_does_not_silently_fall_back_to_cpu(self):
        fake = SimpleNamespace(get_cuda_device_count=lambda: 0)
        with mock.patch.dict(sys.modules, {'ctranslate2': fake}):
            with self.assertRaises(media_stage_manager.WorkflowError):
                media_stage_manager.resolve_device({}, None, None)
            self.assertEqual(media_stage_manager.resolve_device({}, 'cpu', None), ('cpu', 'int8'))

    def test_continuous_runner_respects_staged_gate(self):
        with mock.patch.object(media_stage_manager, 'resolve_input', return_value=(Path('x.mp3'), Path('.'))), \
             mock.patch.object(media_stage_manager, 'choose_media', return_value=Path('x.mp3')), \
             mock.patch.object(media_stage_manager, 'load_state', return_value={'execution': {'mode': 'staged'}}):
            with self.assertRaises(media_stage_manager.WorkflowError):
                media_stage_manager.command_run_continuous(SimpleNamespace(input=Path('x.mp3'), database_root=None))


if __name__ == '__main__':
    unittest.main()
