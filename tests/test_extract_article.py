"""文章机械提取脚本的离线单元测试。

测试全部使用临时目录创建小型来源文件，不访问网络、不读取真实资料资料库，也不
依赖本机安装 Word。PDF 解析通过最小假对象验证文本层、扫描版和依赖错误分支，
从而让测试环境无需为了单测额外下载 pypdf。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from email.message import EmailMessage
from email import policy
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, path: Path):
    """从 scripts 目录加载单文件脚本，保持与用户实际调用布局一致。"""

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载测试模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


article = load_module("extract_article", SKILL_ROOT / "scripts" / "extract_article.py")


class HTMLExtractionTests(unittest.TestCase):
    """验证 HTML 元数据、伴随资源和转发归因。"""

    def test_html_repost_comment_metadata_resources_and_original_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "发布者转发.html"
            original = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta property="og:title" content="一篇需要归档的文章">
<meta property="og:url" content="https://example.test/post/42">
<meta name="author" content="发布账号甲">
<meta name="publisher" content="当前发布平台甲">
<meta property="article:published_time" content="2026-08-28 09:30">
</head><body>
<nav>首页 广告 登录</nav>
<article>
  <p>编者按：这一段是转发者明确添加的说明。</p>
  <p>以下为原文：</p>
  <p>原作者：原作者乙</p>
  <p>原作者提出了第一个观点。</p>
  <p>原作者又给出了具体论据。</p>
  <img src="发布者转发_files/chart.png">
</article>
<footer>无关页脚</footer>
</body></html>""".encode("utf-8")
            source.write_bytes(original)
            resources = root / "发布者转发_files"
            resources.mkdir()
            (resources / "chart.png").write_bytes(b"not-a-real-image-but-an-archived-resource")

            outcome = article.extract_file(source)

            self.assertEqual(source.read_bytes(), original, "机械提取不得改动原 HTML")
            self.assertEqual(outcome["status"], "created")
            result_dir = root / "发布者转发_处理结果"
            metadata = json.loads((result_dir / article.METADATA_FILENAME).read_text(encoding="utf-8"))
            markdown = (result_dir / article.BODY_FILENAME).read_text(encoding="utf-8")

            self.assertEqual(metadata["title"], "一篇需要归档的文章")
            self.assertEqual(metadata["original_url"], "https://example.test/post/42")
            self.assertEqual(metadata["author_or_account"], "发布账号甲")
            self.assertEqual(metadata["page_author_or_account"], "发布账号甲")
            self.assertEqual(metadata["current_publisher"], "当前发布平台甲")
            self.assertEqual(metadata["original_author"], "原作者乙")
            self.assertEqual(metadata["published_at"], "2026-08-28 09:30")
            self.assertEqual(metadata["source_type"], "repost_with_comment")
            self.assertEqual(metadata["reposter_comment"], "这一段是转发者明确添加的说明。")
            self.assertEqual(metadata["companion_resource_directories"][0]["directory_name"], "发布者转发_files")
            self.assertEqual(metadata["companion_resource_directories"][0]["file_count"], 1)
            self.assertEqual(metadata["missing_local_resource_references"], [])
            self.assertEqual(
                metadata["output_sha256"][article.BODY_FILENAME],
                article.sha256_file(result_dir / article.BODY_FILENAME),
            )
            self.assertIn("## 转发者附言", markdown)
            self.assertIn("## 原文正文", markdown)
            self.assertIn("- 当前发布者：当前发布平台甲", markdown)
            self.assertIn("- 原作者：原作者乙", markdown)
            self.assertIn("转发行为本身不代表发布账号完全赞同", markdown)
            self.assertIn("原作者提出了第一个观点", markdown)
            self.assertNotIn("首页 广告 登录", markdown)
            self.assertNotIn("无关页脚", markdown)
            self.assertEqual(list(result_dir.glob(".*.tmp.*")), [])

    def test_plain_repost_does_not_invent_comment_or_endorsement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "plain.htm"
            source.write_text(
                "<html><body><main><p>转载自：原作者乙</p><p>这只是原作者的主张。</p></main></body></html>",
                encoding="utf-8",
            )
            article.extract_file(source)
            result_dir = source.parent / "plain_处理结果"
            metadata = json.loads((result_dir / article.METADATA_FILENAME).read_text(encoding="utf-8"))
            markdown = (result_dir / article.BODY_FILENAME).read_text(encoding="utf-8")
            self.assertEqual(metadata["source_type"], "repost")
            self.assertIsNone(metadata["reposter_comment"])
            self.assertIn("不代表发布账号完全赞同", metadata["attribution_notice"])
            self.assertNotIn("转载自：原作者乙\n\n这只是", markdown)
            self.assertIn("这只是原作者的主张", markdown)

    def test_unmarked_article_remains_unknown(self) -> None:
        result = article.analyze_attribution("某账号发布了一段很鲜明的观点，但页面没有任何来源标记。")
        self.assertEqual(result["source_type"], "unknown")
        self.assertIsNone(result["reposter_comment"])
        self.assertIn("不据此推断", result["attribution_notice"])

    def test_current_publisher_is_never_copied_to_original_author(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "publisher-only.html"
            source.write_text(
                '<html><head><meta name="publisher" content="页面账号甲"></head>'
                '<body><main><p>页面账号：页面账号甲</p><p>这里没有原作者署名。</p></main></body></html>',
                encoding="utf-8",
            )
            article.extract_file(source)
            result_dir = source.parent / "publisher-only_处理结果"
            metadata = json.loads((result_dir / article.METADATA_FILENAME).read_text(encoding="utf-8"))
            markdown = (result_dir / article.BODY_FILENAME).read_text(encoding="utf-8")
            self.assertEqual(metadata["current_publisher"], "页面账号甲")
            self.assertIsNone(metadata["original_author"])
            self.assertIn("- 当前发布者：页面账号甲", markdown)
            self.assertIn("- 原作者：未识别", markdown)

    def test_inline_comment_without_later_boundary_does_not_consume_original(self) -> None:
        result = article.analyze_attribution(
            "转载自：原作者\n\n编者按：值得留意。\n\n原文第一段。\n\n原文第二段。"
        )
        self.assertEqual(result["source_type"], "repost_with_comment")
        self.assertEqual(result["reposter_comment"], "值得留意。")
        self.assertIn("原文第一段", result["original_body"])
        self.assertIn("原文第二段", result["original_body"])
        self.assertNotIn("值得留意", result["original_body"])

    def test_dot_files_companion_directory_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "saved.html"
            source.write_text("<html><body><article>足够长度的正文内容，用来验证点 files 目录。</article></body></html>", encoding="utf-8")
            companion = root / "saved.files"
            companion.mkdir()
            (companion / "style.css").write_text("body{}", encoding="utf-8")
            metadata = article.describe_resource_directories(source)
            self.assertEqual(metadata[0]["directory_name"], "saved.files")
            self.assertEqual(metadata[0]["files"][0]["relative_path"], "style.css")


class OtherFormatTests(unittest.TestCase):
    """验证 MHTML、TXT/MD、DOCX 与 PDF 的离线分支。"""

    def test_mhtml_extracts_html_part_and_content_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "saved.mhtml"
            message = EmailMessage()
            message["Subject"] = "MHTML 备用标题"
            message.set_content("纯文本备用内容")
            message.add_alternative(
                '<html><head><meta property="og:title" content="MHTML 主标题"></head>'
                '<body><article><p>原文来自：某站</p><p>MHTML 中的正文内容。</p></article></body></html>',
                subtype="html",
                charset="utf-8",
            )
            html_part = message.get_payload()[-1]
            html_part["Content-Location"] = "https://example.test/archive"
            source.write_bytes(message.as_bytes(policy=policy.default))

            article.extract_file(source)
            metadata = json.loads(
                (source.parent / "saved_处理结果" / article.METADATA_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["title"], "MHTML 主标题")
            self.assertEqual(metadata["original_url"], "https://example.test/archive")
            self.assertEqual(metadata["source_type"], "repost")

    def test_markdown_front_matter_and_text_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "note.md"
            markdown.write_text(
                "---\ntitle: 资料标题\nauthor: 作者丙\ndate: 2026-08-27\nurl: https://example.test/md\n---\n"
                "# 正文内标题\n\n- 列表\n\n    保留四空格代码块\n",
                encoding="utf-8",
            )
            text = root / "legacy.txt"
            text.write_bytes("作者：作者丁\n发布时间：2026年8月26日\n正文。".encode("gb18030"))

            markdown_result = article.extract_source(markdown)
            text_result = article.extract_source(text)
            self.assertEqual(markdown_result["title"], "资料标题")
            self.assertEqual(markdown_result["author_or_account"], "作者丙")
            self.assertNotIn("title: 资料标题", markdown_result["body"])
            self.assertIn("    保留四空格代码块", markdown_result["body"])
            attribution = article.analyze_attribution(markdown_result["body"])
            self.assertIn("    保留四空格代码块", attribution["original_body"])
            self.assertEqual(text_result["author_or_account"], "作者丁")
            self.assertEqual(text_result["detected_encoding"].lower(), "gb18030")

    def test_docx_extracts_core_properties_and_paragraphs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "article.docx"
            document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>第一段正文。</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格中的文字。</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
  </w:body>
</w:document>"""
            core_xml = """<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">
 <dc:title>Word 标题</dc:title><dc:creator>Word 作者</dc:creator>
 <dcterms:created>2026-08-25T12:00:00Z</dcterms:created>
</cp:coreProperties>"""
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("word/document.xml", document_xml)
                archive.writestr("docProps/core.xml", core_xml)

            extracted = article.extract_docx(source)
            self.assertEqual(extracted["title"], "Word 标题")
            self.assertEqual(extracted["author_or_account"], "Word 作者")
            self.assertIn("第一段正文", extracted["body"])
            self.assertIn("表格中的文字", extracted["body"])

    def test_pdf_text_layer_and_scanned_pdf_error(self) -> None:
        class FakePage:
            def __init__(self, text):
                self.text = text

            def extract_text(self):
                return self.text

        class TextReader:
            is_encrypted = False

            def __init__(self, _path):
                self.pages = [FakePage("PDF 第一页正文。"), FakePage("PDF 第二页正文。")]
                self.metadata = {"/Title": "PDF 标题", "/Author": "PDF 作者"}

        class ScanReader:
            is_encrypted = False

            def __init__(self, _path):
                self.pages = [FakePage(None), FakePage("   ")]
                self.metadata = {}

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "document.pdf"
            source.write_bytes(b"fake-pdf-for-reader-double")
            with mock.patch.object(article, "_load_pdf_reader", return_value=TextReader):
                extracted = article.extract_pdf(source)
            self.assertEqual(extracted["title"], "PDF 标题")
            self.assertIn("PDF 第二页正文", extracted["body"])

            with mock.patch.object(article, "_load_pdf_reader", return_value=ScanReader):
                with self.assertRaisesRegex(article.OCRRequiredError, "需要先执行 OCR"):
                    article.extract_pdf(source)

    def test_pdf_missing_dependency_has_install_instruction(self) -> None:
        with mock.patch.object(article.importlib, "import_module", side_effect=ImportError("not installed")):
            with self.assertRaisesRegex(article.DependencyMissingError, "pip install pypdf"):
                article._load_pdf_reader()


class BatchAndSafetyTests(unittest.TestCase):
    """验证批处理、复用、冲突保护和 CLI 退出状态。"""

    def test_directory_batch_creates_independent_results_and_skips_result_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("第一份正文。", encoding="utf-8")
            nested = root / "nested"
            nested.mkdir()
            (nested / "b.md").write_text("# 第二份标题\n\n第二份正文。", encoding="utf-8")
            fake_result = root / "old_处理结果"
            fake_result.mkdir()
            (fake_result / "should_skip.txt").write_text("不应作为新来源。", encoding="utf-8")

            summary = article.process_input(root)
            self.assertEqual(summary["source_count"], 2)
            self.assertEqual(summary["success_count"], 2)
            self.assertEqual(summary["failure_count"], 0)
            self.assertTrue((root / "a_处理结果" / article.BODY_FILENAME).is_file())
            self.assertTrue((nested / "b_处理结果" / article.BODY_FILENAME).is_file())
            self.assertFalse((fake_result / "should_skip_处理结果").exists())

    def test_complete_same_hash_result_is_reused_and_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "stable.txt"
            original = b"stable source content"
            source.write_bytes(original)
            first = article.extract_file(source)
            second = article.extract_file(source)
            self.assertEqual(first["status"], "created")
            self.assertEqual(second["status"], "reused")
            self.assertEqual(source.read_bytes(), original)

            body_path = source.parent / "stable_处理结果" / article.BODY_FILENAME
            body_path.write_text("人工篡改", encoding="utf-8")
            with self.assertRaisesRegex(article.ExtractionError, "哈希不符"):
                article.extract_file(source)

    def test_same_stem_different_extensions_are_reported_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "same.txt").write_text("文本一", encoding="utf-8")
            (root / "same.md").write_text("文本二", encoding="utf-8")
            with self.assertRaisesRegex(article.ExtractionError, "同一结果目录"):
                article.process_input(root)
            self.assertFalse((root / "same_处理结果").exists())

    def test_scan_pdf_failure_does_not_create_empty_success(self) -> None:
        class EmptyPage:
            def extract_text(self):
                return ""

        class ScanReader:
            is_encrypted = False

            def __init__(self, _path):
                self.pages = [EmptyPage()]
                self.metadata = {}

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "scan.pdf"
            source.write_bytes(b"scan-placeholder")
            with mock.patch.object(article, "_load_pdf_reader", return_value=ScanReader):
                summary = article.process_input(source)
            self.assertEqual(summary["failure_count"], 1)
            self.assertEqual(summary["errors"][0]["error_type"], "OCRRequiredError")
            self.assertIn("OCR", summary["errors"][0]["error"])
            self.assertFalse((source.parent / "scan_处理结果").exists())

    def test_supported_suffix_contract_and_cli_json(self) -> None:
        self.assertEqual(
            article.SUPPORTED_SUFFIXES,
            {".html", ".htm", ".mhtml", ".mht", ".txt", ".md", ".docx", ".pdf"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "cli.txt"
            source.write_text("命令行正文。", encoding="utf-8")
            with mock.patch("builtins.print") as printed:
                exit_code = article.main([str(source), "--json"])
            self.assertEqual(exit_code, 0)
            output = printed.call_args_list[-1].args[0]
            self.assertEqual(json.loads(output)["success_count"], 1)


if __name__ == "__main__":
    unittest.main()
