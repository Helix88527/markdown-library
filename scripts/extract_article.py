"""机械提取本地文章，并生成可追溯的 Markdown 原始正文。

这个脚本只负责文字资料工作流的第一步：读取用户已经保存到本地的来源文件，
提取可见正文及来源元数据，并在原件旁建立独立结果目录。它不会校订作者观点，
不会联网，也不会修改、移动或删除原件及网页伴随资源。

支持的输入格式是 HTML/HTM、MHTML/MHT、TXT、Markdown、DOCX 和 PDF。
HTML、MHTML、TXT、Markdown 与 DOCX 均只使用 Python 标准库；PDF 需要运行
环境提供 ``pypdf``（兼容回退到 ``PyPDF2``）。扫描 PDF 没有文本层时会明确
要求 OCR，而不会写出空正文冒充成功。

命令示例::

    python scripts/extract_article.py D:\资料\文章.html
    python scripts/extract_article.py D:\资料\待处理文章 --json

每个来源默认写入同目录下的 ``<stem>_处理结果``，并生成：

* ``00_资料信息.json``：来源身份、SHA256、页面元数据与归因判断；
* ``01_原始正文.md``：机械提取正文及明确的原创/转发边界。

写入采用“同目录临时文件 + fsync + os.replace”的原子提交方式。已有完整结果
且来源 SHA256 相同则默认复用；来源已变化或结果不完整时拒绝静默覆盖，只有调用
者明确给出 ``--force`` 才会重新生成机械提取成果。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree


SUPPORTED_SUFFIXES = {
    ".html",
    ".htm",
    ".mhtml",
    ".mht",
    ".txt",
    ".md",
    ".docx",
    ".pdf",
}
RESULT_SUFFIX = "_处理结果"
METADATA_FILENAME = "00_资料信息.json"
BODY_FILENAME = "01_原始正文.md"
SCHEMA_VERSION = "1.1"


class ExtractionError(RuntimeError):
    """用户可理解、无需显示 Python 堆栈的提取失败。"""


class DependencyMissingError(ExtractionError):
    """可选格式所需依赖未安装。"""


class OCRRequiredError(ExtractionError):
    """PDF 没有可用文本层，需要先执行 OCR。"""


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA256，避免把大型原件一次性读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """在目标同目录原子写入完整字节串。

    临时文件必须与目标位于同一目录，以便 ``os.replace`` 在 Windows 和 POSIX
    上都保持同一文件系统内的原子替换语义。任何异常都会尽力删除临时文件。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.tmp.",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def atomic_write_text(path: Path, text: str) -> None:
    """以 UTF-8、LF 换行原子写入文本。"""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    atomic_write_bytes(path, normalized.encode("utf-8"))


def _charset_from_bytes(payload: bytes) -> str | None:
    """从 HTML 开头的 meta charset 提示中提取编码名称。"""

    head = payload[:8192]
    match = re.search(br"charset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)", head, re.I)
    return match.group(1).decode("ascii", errors="ignore") if match else None


def decode_text_bytes(payload: bytes, declared_charset: str | None = None) -> tuple[str, str]:
    """按 BOM、声明编码和常见中文编码顺序解码文字文件。

    返回 ``(文本, 实际采用的编码)``。只有所有可信候选都失败时才使用 UTF-8
    replacement 模式，并将编码名称标成 ``utf-8-replacement``，方便元数据披露。
    """

    candidates: list[str] = []
    if payload.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    elif payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.append("utf-16")
    for candidate in (declared_charset, _charset_from_bytes(payload), "utf-8-sig", "utf-8", "gb18030", "big5"):
        if candidate and candidate.lower() not in {item.lower() for item in candidates}:
            candidates.append(candidate)

    for encoding in candidates:
        try:
            text = payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
        if "\x00" in text and encoding.lower() not in {"utf-16", "utf-16-le", "utf-16-be"}:
            continue
        return text, encoding
    return payload.decode("utf-8", errors="replace"), "utf-8-replacement"


def normalize_plain_text(text: str) -> str:
    """清理机械提取文本的换行和行尾空白，不改变段落顺序或观点。"""

    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    output: list[str] = []
    blank = False
    for line in lines:
        if line:
            output.append(line)
            blank = False
        elif output and not blank:
            output.append("")
            blank = True
    return "\n".join(output).strip()


def normalize_structured_text(text: str) -> str:
    """规范换行但保留 Markdown 缩进、列表与代码块结构。

    ``normalize_plain_text`` 适合从 HTML/XML 得到的散碎文字，却会移除行首空格。
    对用户提供的 Markdown 以及最终分离出的原文不能这样处理，否则四空格代码块、
    嵌套列表和引用层级会被破坏。这里仅清理行尾空白并折叠连续空行。
    """

    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    output: list[str] = []
    blank = False
    for line in lines:
        if line.strip():
            output.append(line)
            blank = False
        elif output and not blank:
            output.append("")
            blank = True
    return "\n".join(output)


class VisibleHTMLParser(HTMLParser):
    """提取 HTML 可见正文、常见 meta 字段和本地资源引用。

    这里故意不使用 CSS 选择器依赖。解析器分别收集 ``article``、``main`` 与
    全局可见文本，结束后优先采用语义更明确且内容足够长的区域。脚本、导航、
    页脚、广告表单等结构被排除，但不会做可能改变原意的语言层面删改。
    """

    BLOCK_TAGS = {
        "address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
        "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li", "main", "p", "pre",
        "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul", "ol",
    }
    ALWAYS_IGNORED = {"style", "noscript", "template", "svg", "canvas", "nav", "aside", "footer", "form"}
    AUTHOR_TOKEN = re.compile(r"(?:^|[-_\s])(author|byline)(?:$|[-_\s])", re.I)
    ACCOUNT_TOKEN = re.compile(r"(?:^|[-_\s])(account|nickname|profile[-_]?name|js[-_]?name|user[-_]?name)(?:$|[-_\s])", re.I)
    DATE_TOKEN = re.compile(r"(?:publish|post[-_]?date|created|creation|timestamp|article[-_]?time)", re.I)
    TITLE_TOKEN = re.compile(r"(?:article|post|entry|rich[-_]?media)[-_]?(?:title|subject)", re.I)

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.general_parts: list[str] = []
        self.article_parts: list[str] = []
        self.main_parts: list[str] = []
        self.json_ld_parts: list[str] = []
        self.resource_references: list[str] = []
        self.captured: dict[str, list[str]] = {
            "author_candidate": [],
            "current_publisher": [],
            "published_at": [],
            "title": [],
        }
        self._ignore_stack: list[str] = []
        self._head_depth = 0
        self._title_depth = 0
        self._h1_depth = 0
        self._article_depth = 0
        self._main_depth = 0
        self._json_ld_depth = 0
        self._capture_stack: list[dict[str, Any]] = []

    @staticmethod
    def _attrs(attrs: Sequence[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).lower(): (value or "") for key, value in attrs}

    def _append_separator(self) -> None:
        """给各有效缓冲区加入换行，后续会统一折叠多余空行。"""

        if self._head_depth == 0 and not self._ignore_stack:
            self.general_parts.append("\n")
            if self._article_depth:
                self.article_parts.append("\n")
            if self._main_depth:
                self.main_parts.append("\n")

    def handle_starttag(self, tag: str, attrs: Sequence[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = self._attrs(attrs)
        if tag == "head":
            self._head_depth += 1
        if tag == "title":
            self._title_depth += 1
        if tag == "h1":
            self._h1_depth += 1
        if tag == "article":
            self._article_depth += 1
        if tag == "main":
            self._main_depth += 1

        if tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or attributes.get("itemprop") or "").strip().lower()
            content = attributes.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "link":
            relation = attributes.get("rel", "").lower().split()
            href = attributes.get("href", "").strip()
            if "canonical" in relation and href:
                self.meta.setdefault("canonical", href)

        for attribute in ("src", "href", "poster", "data-src"):
            value = attributes.get(attribute, "").strip()
            if value and value not in self.resource_references:
                self.resource_references.append(value)

        if tag == "script" and "ld+json" in attributes.get("type", "").lower():
            self._json_ld_depth += 1
        elif tag in self.ALWAYS_IGNORED or tag == "script":
            self._ignore_stack.append(tag)

        identity = " ".join((attributes.get("id", ""), attributes.get("class", ""), attributes.get("name", ""))).strip()
        capture_kind: str | None = None
        if self.ACCOUNT_TOKEN.search(identity):
            capture_kind = "current_publisher"
        elif self.AUTHOR_TOKEN.search(identity):
            capture_kind = "author_candidate"
        elif self.DATE_TOKEN.search(identity):
            capture_kind = "published_at"
        elif self.TITLE_TOKEN.search(identity):
            capture_kind = "title"
        if tag == "time" and attributes.get("datetime"):
            self.captured["published_at"].append(attributes["datetime"].strip())
        if capture_kind:
            self._capture_stack.append({"tag": tag, "kind": capture_kind, "parts": []})

        if tag in self.BLOCK_TAGS:
            self._append_separator()

    def handle_startendtag(self, tag: str, attrs: Sequence[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._json_ld_depth:
            self.json_ld_parts.append(data)
            return
        if self._title_depth:
            self.title_parts.append(data)
        if self._h1_depth and not self._ignore_stack:
            self.h1_parts.append(data)
        for capture in self._capture_stack:
            capture["parts"].append(data)
        if self._head_depth or self._ignore_stack:
            return
        self.general_parts.append(data)
        if self._article_depth:
            self.article_parts.append(data)
        if self._main_depth:
            self.main_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.BLOCK_TAGS:
            self._append_separator()

        for index in range(len(self._capture_stack) - 1, -1, -1):
            capture = self._capture_stack[index]
            if capture["tag"] == tag:
                value = normalize_plain_text("".join(capture["parts"]))
                if value and value not in self.captured[capture["kind"]]:
                    self.captured[capture["kind"]].append(value)
                del self._capture_stack[index]
                break

        if self._ignore_stack and self._ignore_stack[-1] == tag:
            self._ignore_stack.pop()
        if tag == "script" and self._json_ld_depth:
            self._json_ld_depth -= 1
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag == "h1" and self._h1_depth:
            self._h1_depth -= 1
        if tag == "article" and self._article_depth:
            self._article_depth -= 1
        if tag == "main" and self._main_depth:
            self._main_depth -= 1
        if tag == "head" and self._head_depth:
            self._head_depth -= 1

    def visible_body(self) -> str:
        """优先返回 article/main 语义区域，否则返回全局可见正文。"""

        article = normalize_plain_text("".join(self.article_parts))
        main = normalize_plain_text("".join(self.main_parts))
        general = normalize_plain_text("".join(self.general_parts))
        if len(article) >= 20:
            return article
        if len(main) >= 20:
            return main
        return general


def _first_nonempty(values: Iterable[Any]) -> str | None:
    """把不同元数据来源归一化为首个非空字符串。"""

    for value in values:
        if isinstance(value, dict):
            value = value.get("name") or value.get("@id")
        if isinstance(value, list):
            nested = _first_nonempty(value)
            if nested:
                return nested
        elif value is not None and str(value).strip():
            return str(value).strip()
    return None


def _walk_json_objects(value: Any) -> Iterable[dict[str, Any]]:
    """递归遍历 JSON-LD 中的对象，兼容 ``@graph`` 与数组包装。"""

    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_objects(child)


def _json_ld_metadata(chunks: Sequence[str]) -> dict[str, str]:
    """从可解析的 JSON-LD 中提取文章标题、作者、日期和 URL。"""

    result: dict[str, str] = {}
    raw = "\n".join(chunks).strip()
    if not raw:
        return result
    try:
        payload = json.loads(unescape(raw))
    except (json.JSONDecodeError, TypeError):
        return result
    for item in _walk_json_objects(payload):
        item_type = str(item.get("@type", "")).lower()
        if item_type and not any(word in item_type for word in ("article", "posting", "news", "webpage")):
            continue
        result.setdefault("title", _first_nonempty((item.get("headline"), item.get("name"))) or "")
        result.setdefault("url", _first_nonempty((item.get("url"), item.get("mainEntityOfPage"))) or "")
        result.setdefault("author", _first_nonempty((item.get("author"), item.get("creator"))) or "")
        result.setdefault("published_at", _first_nonempty((item.get("datePublished"), item.get("dateCreated"))) or "")
    return {key: value for key, value in result.items() if value}


def _extract_html_text(text: str, fallback_url: str | None = None) -> dict[str, Any]:
    """解析一个已经解码的 HTML 文档。"""

    parser = VisibleHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # HTMLParser 对残缺网页通常容错，此处保留清楚错误边界。
        raise ExtractionError(f"HTML 结构无法解析：{exc}") from exc

    body = parser.visible_body()
    if not body:
        raise ExtractionError("HTML 中没有提取到可见正文；请检查网页是否只包含脚本或外部动态内容。")
    ld = _json_ld_metadata(parser.json_ld_parts)
    meta = parser.meta
    title = _first_nonempty(
        (
            meta.get("og:title"), meta.get("twitter:title"), ld.get("title"),
            parser.captured["title"], normalize_plain_text("".join(parser.h1_parts)),
            normalize_plain_text("".join(parser.title_parts)),
        )
    )
    # ``author``/JSON-LD author 在不同平台可能指原作者，也可能只是页面署名，因而
    # 只作为歧义候选披露。当前发布者只取 publisher/site/account 等平台字段；
    # 原作者则只接受 original_author 之类明确字段或正文中的“原作者：”标签。
    author_candidate = _first_nonempty(
        (
            meta.get("author"), meta.get("article:author"), meta.get("og:article:author"),
            meta.get("og:author"), meta.get("byline"), meta.get("byl"),
            ld.get("author"), parser.captured["author_candidate"],
        )
    )
    line_meta = _line_metadata(body)
    current_publisher = _first_nonempty(
        (
            meta.get("publisher"), meta.get("og:site_name"), meta.get("account"),
            meta.get("account_name"), parser.captured["current_publisher"],
            line_meta.get("current_publisher"),
        )
    )
    original_author = _first_nonempty(
        (
            meta.get("original_author"), meta.get("original-author"),
            meta.get("article:original_author"), line_meta.get("original_author"),
        )
    )
    published = _first_nonempty(
        (
            meta.get("article:published_time"), meta.get("datepublished"), meta.get("date"),
            meta.get("pubdate"), meta.get("publishdate"), ld.get("published_at"),
            parser.captured["published_at"],
        )
    )
    url = _first_nonempty((meta.get("canonical"), meta.get("og:url"), ld.get("url"), fallback_url))
    return {
        "title": title,
        "author_or_account": author_candidate,
        "page_author_or_account": author_candidate,
        "current_publisher": current_publisher,
        "original_author": original_author,
        "published_at": published,
        "original_url": url,
        "body": body,
        "resource_references": parser.resource_references,
        "html_meta": meta,
    }


def extract_html(path: Path) -> dict[str, Any]:
    """提取普通 HTML/HTM；支持 UTF-8、常见中文编码及 meta charset。"""

    payload = path.read_bytes()
    text, encoding = decode_text_bytes(payload)
    result = _extract_html_text(text)
    result["detected_encoding"] = encoding
    return result


def _decoded_email_part(part: Any) -> tuple[str, str]:
    """可靠解码 MHTML 的一个 text part，并返回所用编码。"""

    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return (str(raw) if raw is not None else ""), part.get_content_charset() or "unicode"
    return decode_text_bytes(payload, part.get_content_charset())


def extract_mhtml(path: Path) -> dict[str, Any]:
    """从 MHTML/MHT 中选择首个 HTML 主文档，必要时回退到纯文本 part。"""

    try:
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    except Exception as exc:
        raise ExtractionError(f"MHTML 邮件封装无法解析：{exc}") from exc
    html_part = None
    text_part = None
    for part in message.walk():
        content_type = part.get_content_type().lower()
        if content_type == "text/html" and html_part is None:
            html_part = part
        elif content_type == "text/plain" and text_part is None:
            text_part = part
    subject = str(message.get("Subject", "")).strip() or None
    if html_part is not None:
        text, encoding = _decoded_email_part(html_part)
        location = str(html_part.get("Content-Location", "")).strip() or str(message.get("Content-Location", "")).strip() or None
        result = _extract_html_text(text, fallback_url=location)
        result["detected_encoding"] = encoding
        result["title"] = result.get("title") or subject
        return result
    if text_part is not None:
        text, encoding = _decoded_email_part(text_part)
        body = normalize_plain_text(text)
        if not body:
            raise ExtractionError("MHTML 的纯文本部分为空。")
        return {
            "title": subject,
            "author_or_account": None,
            "page_author_or_account": None,
            "current_publisher": _line_metadata(body).get("current_publisher"),
            "original_author": _line_metadata(body).get("original_author"),
            "published_at": None,
            "original_url": str(message.get("Content-Location", "")).strip() or None,
            "body": body,
            "resource_references": [],
            "detected_encoding": encoding,
            "html_meta": {},
        }
    raise ExtractionError("MHTML 中没有找到 text/html 或 text/plain 主文档。")


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    """读取简单 Markdown YAML front matter；不引入 PyYAML 依赖。"""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, normalized
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return {}, normalized
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip().strip("'\"")
        if key and value:
            metadata[key] = value
    return metadata, "\n".join(lines[end + 1 :])


def _line_metadata(body: str) -> dict[str, str]:
    """从纯文本或 Markdown 开头识别保守的标题、作者、日期和 URL。"""

    result: dict[str, str] = {}
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    for line in lines[:30]:
        if "title" not in result:
            match = re.match(r"^#{1,6}\s+(.+)$", line)
            if match:
                result["title"] = match.group(1).strip()
        match = re.match(r"^(?:原作者|原文作者|original author)\s*[:：]\s*(.+)$", line, re.I)
        if match and "original_author" not in result:
            result["original_author"] = match.group(1).strip()
        match = re.match(r"^(?:当前发布者|发布账号|页面账号|转发账号|账号|公众号|publisher)\s*[:：]\s*(.+)$", line, re.I)
        if match and "current_publisher" not in result:
            result["current_publisher"] = match.group(1).strip()
        match = re.match(r"^(?:作者|author)\s*[:：]\s*(.+)$", line, re.I)
        if match and "author" not in result:
            result["author"] = match.group(1).strip()
        match = re.match(r"^(?:发布时间|发布日期|日期|published|date)\s*[:：]\s*(.+)$", line, re.I)
        if match and "published_at" not in result:
            result["published_at"] = match.group(1).strip()
        if "url" not in result:
            match = re.search(r"https?://[^\s)>\]]+", line)
            if match:
                result["url"] = match.group(0).rstrip(".,;，。；")
    return result


def extract_text_or_markdown(path: Path) -> dict[str, Any]:
    """读取 TXT/MD，保留 Markdown 结构并解析常见头部元数据。"""

    text, encoding = decode_text_bytes(path.read_bytes())
    front, body = _front_matter(text) if path.suffix.lower() == ".md" else ({}, text)
    body = normalize_structured_text(body) if path.suffix.lower() == ".md" else normalize_plain_text(body)
    if not body:
        raise ExtractionError(f"{path.suffix.upper()} 文件没有可提取正文。")
    lines = _line_metadata(body)
    return {
        "title": _first_nonempty((front.get("title"), lines.get("title"))),
        "author_or_account": _first_nonempty((front.get("author"), front.get("account"), lines.get("author"))),
        "page_author_or_account": _first_nonempty((front.get("author"), lines.get("author"))),
        "current_publisher": _first_nonempty(
            (front.get("publisher"), front.get("account"), lines.get("current_publisher"))
        ),
        "original_author": _first_nonempty((front.get("original_author"), lines.get("original_author"))),
        "published_at": _first_nonempty((front.get("date"), front.get("published_at"), lines.get("published_at"))),
        "original_url": _first_nonempty((front.get("url"), front.get("source_url"), lines.get("url"))),
        "body": body,
        "resource_references": [],
        "detected_encoding": encoding,
        "html_meta": {},
    }


def _docx_core_properties(archive: zipfile.ZipFile) -> dict[str, str]:
    """读取 DOCX Dublin Core 属性；缺失属性不是错误。"""

    try:
        raw = archive.read("docProps/core.xml")
    except KeyError:
        return {}
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return {}
    local_map = {element.tag.rsplit("}", 1)[-1].lower(): (element.text or "").strip() for element in root.iter()}
    return {
        "title": local_map.get("title", ""),
        "author": local_map.get("creator", ""),
        "published_at": local_map.get("created", ""),
    }


def extract_docx(path: Path) -> dict[str, Any]:
    """从 DOCX 主文档按 XML 顺序提取段落与表格单元格文字。"""

    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("word/document.xml")
            core = _docx_core_properties(archive)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ExtractionError(f"DOCX 文件损坏或缺少 word/document.xml：{exc}") from exc
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ExtractionError(f"DOCX 主文档 XML 无法解析：{exc}") from exc

    paragraphs: list[str] = []
    for paragraph in root.iter():
        if paragraph.tag.rsplit("}", 1)[-1] != "p":
            continue
        pieces: list[str] = []
        for node in paragraph.iter():
            local_name = node.tag.rsplit("}", 1)[-1]
            if local_name == "t":
                pieces.append(node.text or "")
            elif local_name == "tab":
                pieces.append("\t")
            elif local_name == "br":
                pieces.append("\n")
        value = "".join(pieces).strip()
        if value:
            paragraphs.append(value)
    body = normalize_plain_text("\n\n".join(paragraphs))
    if not body:
        raise ExtractionError("DOCX 中没有提取到正文；图片式 Word 需要先执行 OCR。")
    line_meta = _line_metadata(body)
    return {
        "title": _first_nonempty((core.get("title"), line_meta.get("title"))),
        "author_or_account": _first_nonempty((core.get("author"), line_meta.get("author"))),
        "page_author_or_account": _first_nonempty((core.get("author"), line_meta.get("author"))),
        "current_publisher": line_meta.get("current_publisher"),
        "original_author": line_meta.get("original_author"),
        "published_at": _first_nonempty((core.get("published_at"), line_meta.get("published_at"))),
        "original_url": line_meta.get("url"),
        "body": body,
        "resource_references": [],
        "detected_encoding": "docx-xml",
        "html_meta": {},
    }


def _load_pdf_reader() -> Any:
    """延迟加载 PDF 依赖，使其他格式在未安装 pypdf 时仍可正常工作。"""

    errors: list[str] = []
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = importlib.import_module(module_name)
            return module.PdfReader
        except (ImportError, AttributeError) as exc:
            errors.append(f"{module_name}: {exc}")
    detail = "; ".join(errors)
    raise DependencyMissingError(
        "处理 PDF 需要可提取文本层的 PDF 解析依赖。请在本 Skill 的固定 Python 环境执行 "
        "`python -m pip install pypdf` 后重试。依赖检查详情：" + detail
    )


def extract_pdf(path: Path) -> dict[str, Any]:
    """提取 PDF 文本层；空文本层明确转为 OCRRequiredError。"""

    reader_class = _load_pdf_reader()
    try:
        reader = reader_class(str(path))
        if getattr(reader, "is_encrypted", False):
            try:
                unlocked = reader.decrypt("")
            except Exception:
                unlocked = 0
            if not unlocked:
                raise ExtractionError("PDF 已加密且无法用空密码读取。")
        pages: list[str] = []
        for index, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception as exc:
                raise ExtractionError(f"PDF 第 {index} 页文本提取失败：{exc}") from exc
            page_text = normalize_plain_text(page_text)
            if page_text:
                pages.append(page_text)
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"PDF 无法解析：{exc}") from exc

    body = normalize_plain_text("\n\n".join(pages))
    if not body:
        raise OCRRequiredError(
            "PDF 没有可提取的文本层，可能是扫描版。需要先执行 OCR，再重新运行文章提取；"
            "本次未生成空的 01_原始正文.md。"
        )
    metadata = getattr(reader, "metadata", None) or {}
    def pdf_meta(*keys: str) -> str | None:
        values = []
        for key in keys:
            try:
                values.append(metadata.get(key))
            except AttributeError:
                values.append(getattr(metadata, key.lstrip("/").lower(), None))
        return _first_nonempty(values)

    line_meta = _line_metadata(body)
    return {
        "title": _first_nonempty((pdf_meta("/Title", "title"), line_meta.get("title"))),
        "author_or_account": _first_nonempty((pdf_meta("/Author", "author"), line_meta.get("author"))),
        "page_author_or_account": _first_nonempty((pdf_meta("/Author", "author"), line_meta.get("author"))),
        "current_publisher": line_meta.get("current_publisher"),
        "original_author": line_meta.get("original_author"),
        "published_at": _first_nonempty((pdf_meta("/CreationDate", "creation_date"), line_meta.get("published_at"))),
        "original_url": line_meta.get("url"),
        "body": body,
        "resource_references": [],
        "detected_encoding": "pdf-text-layer",
        "html_meta": {},
    }


COMMENT_LABEL = re.compile(
    r"^(?:[>*#\-\s]*)?(?:转发者|发布者)?(?:附言|按语|评论|导语)|^(?:[>*#\-\s]*)?(?:编者按|转发语|我的评论)\s*[:：]?",
    re.I,
)
ORIGINAL_BOUNDARY = re.compile(
    r"^(?:[>*#\-\s]*)?(?:以下(?:为|是)?原文|原文(?:如下|正文)?|转载正文|转发正文)\s*[:：]?\s*$",
    re.I,
)
REPOST_MARKER = re.compile(
    r"^(?:[>*#\-\s\[【]*)?(?:本文\s*)?(?:转发|转载)(?:自|于)?(?:\s*[:：].*|\s*)$"
    r"|^(?:[>*#\-\s\[【]*)?(?:转自|转载自|原文(?:来自|来源于|链接)|内容来源)\s*[:：]?.*$"
    r"|^(?:[>*#\-\s\[【]*)?来源\s*[:：]\s*(?!原创\b|本号\b).+$",
    re.I,
)
ORIGINAL_MARKER = re.compile(r"^(?:[>*#\-\s\[【]*)?(?:原创|本文原创|作者原创)\s*(?:[]】\]]|[:：].*)?$", re.I)


def _strip_comment_label(line: str) -> str:
    """只移除已识别的附言标签，保留标签后实际文字。"""

    return re.sub(
        r"^(?:[>*#\-\s]*)?(?:(?:转发者|发布者)?(?:附言|按语|评论|导语)|编者按|转发语|我的评论)\s*[:：]?\s*",
        "",
        line,
        count=1,
        flags=re.I,
    ).strip()


def analyze_attribution(body: str) -> dict[str, Any]:
    """保守判断原创/转发，并分离能可靠识别的转发者附言。

    只有明确的“转发、转载、原文来自”等行级标记才判为 repost；只有明确
    “原创”标记才判为 original，其余一律 unknown。附言也必须有标签，或位于
    明确“以下为原文”边界之前，不能把平台标题、作者信息凭空当成转发者观点。
    """

    lines = body.splitlines()
    repost_indices = [index for index, line in enumerate(lines) if REPOST_MARKER.match(line.strip()) or ORIGINAL_BOUNDARY.match(line.strip())]
    original_indices = [index for index, line in enumerate(lines) if ORIGINAL_MARKER.match(line.strip())]
    comment_indices = [index for index, line in enumerate(lines) if COMMENT_LABEL.match(line.strip())]
    boundary_indices = [index for index, line in enumerate(lines) if ORIGINAL_BOUNDARY.match(line.strip())]
    markers = [lines[index].strip() for index in sorted(set(repost_indices + original_indices)) if lines[index].strip()]

    if repost_indices:
        source_type = "repost"
    elif original_indices:
        source_type = "original"
    else:
        source_type = "unknown"

    comment_lines: list[str] = []
    consumed: set[int] = set()
    if source_type == "repost" and comment_indices:
        for position, start in enumerate(comment_indices):
            # 只有明确的“以下为原文”或后置转载标记才能证明多行附言在何处结束。
            # 若标记位于附言之前且后面没有边界，只接受标签同一行的文字，避免把
            # 紧随其后的原文段落整体误判为转发者评论。
            end_candidates = [index for index in boundary_indices if index > start]
            end_candidates.extend(index for index in repost_indices if index > start)
            next_comment = comment_indices[position + 1] if position + 1 < len(comment_indices) else len(lines)
            first = _strip_comment_label(lines[start])
            if first:
                comment_lines.append(first)
            consumed.add(start)
            if end_candidates:
                end = min(end_candidates + [next_comment, len(lines)])
                for index in range(start + 1, end):
                    if lines[index].strip():
                        comment_lines.append(lines[index].strip())
                    consumed.add(index)
    elif source_type == "repost" and boundary_indices:
        boundary = boundary_indices[0]
        # 只有边界前确有实质性文字时才把它视为附言；孤立标题与来源标记被排除。
        candidates = [
            (index, line.strip()) for index, line in enumerate(lines[:boundary])
            if line.strip() and not REPOST_MARKER.match(line.strip()) and not re.match(r"^#{1,6}\s+", line.strip())
        ]
        if candidates and len(candidates) <= 8:
            comment_lines.extend(value for _, value in candidates)
            consumed.update(index for index, _ in candidates)

    comment = normalize_plain_text("\n".join(comment_lines))
    if source_type == "repost" and comment:
        source_type = "repost_with_comment"

    # 正文优先从明确原文边界后开始；否则只剔除标记与已经确认的附言。
    if boundary_indices:
        original_lines = lines[boundary_indices[0] + 1 :]
    else:
        original_lines = [
            line for index, line in enumerate(lines)
            if index not in consumed and not REPOST_MARKER.match(line.strip()) and not COMMENT_LABEL.match(line.strip())
        ]
    original_body = normalize_structured_text("\n".join(original_lines))
    if not original_body:
        original_body = normalize_structured_text(body)

    if source_type in {"repost", "repost_with_comment"}:
        notice = "来源存在明确转发/转载标记。转发行为本身不代表发布账号完全赞同原文全部观点；原文观点应归属于原作者。"
    elif source_type == "original":
        notice = "来源存在明确原创标记；作者或账号仍以页面元数据和正文署名为准。"
    else:
        notice = "没有找到足够明确的原创或转发标记，来源关系暂记为 unknown，不据此推断发布账号的作者身份或赞同态度。"
    return {
        "source_type": source_type,
        "reposter_comment": comment or None,
        "original_body": original_body,
        "attribution_markers": markers,
        "attribution_notice": notice,
    }


def companion_resource_directories(path: Path) -> list[Path]:
    """查找浏览器常见的 ``<stem>_files`` 与 ``<stem>.files`` 伴随目录。"""

    names = {
        f"{path.stem}_files",
        f"{path.stem}.files",
        f"{path.name}_files",
        f"{path.name}.files",
    }
    return [candidate for name in sorted(names) if (candidate := path.parent / name).is_dir()]


def describe_resource_directories(path: Path) -> list[dict[str, Any]]:
    """为 HTML 伴随资源建立只读清单和 SHA256，便于以后核验网页存档完整性。"""

    descriptions: list[dict[str, Any]] = []
    for directory in companion_resource_directories(path):
        files: list[dict[str, Any]] = []
        total_bytes = 0
        resources: list[Path] = []
        # 不跟随符号链接或 Windows junction，防止一个网页资源目录意外把清单扫描
        # 扩展到资料库之外。链接本身也不作为可验证的永久网页资源计入哈希。
        for root, dirnames, filenames in os.walk(directory, followlinks=False):
            root_path = Path(root)
            dirnames[:] = [name for name in dirnames if not (root_path / name).is_symlink()]
            resources.extend(
                resource for name in filenames
                if (resource := root_path / name).is_file() and not resource.is_symlink()
            )
        for resource in sorted(resources, key=lambda item: str(item).lower()):
            size = resource.stat().st_size
            total_bytes += size
            files.append(
                {
                    "relative_path": resource.relative_to(directory).as_posix(),
                    "size": size,
                    "sha256": sha256_file(resource),
                }
            )
        descriptions.append(
            {
                "directory_name": directory.name,
                "file_count": len(files),
                "total_bytes": total_bytes,
                "files": files,
            }
        )
    return descriptions


def _local_reference_status(source: Path, references: Sequence[str]) -> tuple[list[str], list[str]]:
    """区分网页中的本地资源引用与当前缺失引用，不访问网络。"""

    existing: list[str] = []
    missing: list[str] = []
    for reference in references:
        parsed = urlparse(reference)
        if parsed.scheme.lower() in {"http", "https", "data", "mailto", "javascript", "cid"} or reference.startswith("#"):
            continue
        raw_path = unquote(parsed.path).replace("/", os.sep)
        if not raw_path:
            continue
        candidate = (source.parent / raw_path).resolve()
        target = existing if candidate.exists() else missing
        if reference not in target:
            target.append(reference)
    return existing, missing


def extract_source(path: Path) -> dict[str, Any]:
    """按扩展名路由到对应机械提取器。"""

    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        return extract_html(path)
    if suffix in {".mhtml", ".mht"}:
        return extract_mhtml(path)
    if suffix in {".txt", ".md"}:
        return extract_text_or_markdown(path)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix == ".pdf":
        return extract_pdf(path)
    allowed = ", ".join(sorted(SUPPORTED_SUFFIXES))
    raise ExtractionError(f"不支持的文件格式 {suffix or '<无扩展名>'}；支持：{allowed}")


SOURCE_TYPE_LABELS = {
    "original": "原创",
    "repost": "转发/转载",
    "repost_with_comment": "转发/转载并附评论",
    "unknown": "无法确认",
}


def _markdown_value(value: Any) -> str:
    """避免元数据换行破坏 Markdown 列表结构。"""

    return str(value).replace("\r", " ").replace("\n", " ").strip() if value else "未识别"


def render_markdown(source: Path, extracted: dict[str, Any], attribution: dict[str, Any]) -> str:
    """把机械提取内容渲染为有明确证据边界的 Markdown。"""

    title = extracted.get("title") or source.stem
    source_type = attribution["source_type"]
    lines = [
        f"# {title}",
        "",
        "## 资料来源与归因",
        "",
        f"- 原始文件：`{source.name}`",
        f"- 文件格式：`{source.suffix.lower().lstrip('.').upper()}`",
        f"- 来源性质：{SOURCE_TYPE_LABELS[source_type]} (`{source_type}`)",
        f"- 当前发布者：{_markdown_value(extracted.get('current_publisher'))}",
        f"- 原作者：{_markdown_value(extracted.get('original_author'))}",
        f"- 页面作者／账号候选（字段含义不明时不作归因）：{_markdown_value(extracted.get('page_author_or_account'))}",
        f"- 发布时间：{_markdown_value(extracted.get('published_at'))}",
        f"- 原始网址：{_markdown_value(extracted.get('original_url'))}",
        "",
        f"> 归因提示：{attribution['attribution_notice']}",
        "",
    ]
    if attribution.get("attribution_markers"):
        lines.extend(["### 页面中的明确标记", ""])
        lines.extend(f"- {_markdown_value(marker)}" for marker in attribution["attribution_markers"])
        lines.append("")
    if source_type == "repost_with_comment":
        lines.extend(
            [
                "## 转发者附言",
                "",
                attribution["reposter_comment"],
                "",
                "## 原文正文",
                "",
                attribution["original_body"],
            ]
        )
    elif source_type == "repost":
        lines.extend(["## 原文正文", "", attribution["original_body"]])
    else:
        lines.extend(["## 机械提取正文", "", attribution["original_body"]])
    return "\n".join(lines).rstrip() + "\n"


def result_directory_for(path: Path) -> Path:
    """返回固定的单来源结果目录。"""

    return path.parent / f"{path.stem}{RESULT_SUFFIX}"


def _reuse_if_complete(path: Path, source_hash: str) -> dict[str, Any] | None:
    """来源哈希相同且 00/01 完整时返回复用结果，否则由调用者决定是否强制覆盖。"""

    result_dir = result_directory_for(path)
    metadata_path = result_dir / METADATA_FILENAME
    body_path = result_dir / BODY_FILENAME
    if not result_dir.exists():
        return None
    if not metadata_path.is_file() or not body_path.is_file():
        raise ExtractionError(f"已有结果目录不完整：{result_dir}。请检查后使用 --force 明确重建。")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractionError(f"已有 00_资料信息.json 无法读取：{exc}。请检查后使用 --force 重建。") from exc
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ExtractionError(
            f"已有结果使用旧元数据结构 {metadata.get('schema_version')!r}，当前需要 {SCHEMA_VERSION}。"
            "为避免静默覆盖，请检查后使用 --force 明确重建。"
        )
    if metadata.get("original_sha256") != source_hash:
        raise ExtractionError(
            f"原件与已有结果的 SHA256 不一致：{path.name}。为避免覆盖旧成果，请先核对来源；"
            "确认需要重建时使用 --force。"
        )
    output_hashes = metadata.get("output_sha256")
    expected_body_hash = output_hashes.get(BODY_FILENAME) if isinstance(output_hashes, dict) else None
    if not expected_body_hash or sha256_file(body_path) != expected_body_hash:
        raise ExtractionError(f"已有 01_原始正文.md 与清单哈希不符：{body_path}。请核对后使用 --force。")
    for image in metadata.get("image_candidates", []):
        if image.get("path"):
            asset = result_dir / image["path"]
            if not asset.is_file() or sha256_file(asset) != image.get("sha256"):
                raise ExtractionError(f"已有图片候选缺失或已修改：{asset}；保留人工版本，先核对。")
    return {
        "source": str(path.resolve()),
        "result_directory": str(result_dir.resolve()),
        "status": "reused",
        "source_type": metadata.get("source_type", "unknown"),
        "title": metadata.get("title") or path.stem,
    }


def extract_file(path: Path, *, force: bool = False) -> dict[str, Any]:
    """完整处理一个来源文件并原子提交 00/01 两项成果。

    所有解析和 OCR 检查均在建立结果目录前完成，因此不支持或不可解析的来源不会
    留下“看似成功”的空成果。返回值只包含适合 CLI 汇总的状态信息。
    """

    path = path.expanduser().resolve()
    if not path.is_file():
        raise ExtractionError(f"输入不是可读取文件：{path}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return extract_source(path)  # 统一产生清楚的不支持格式错误。
    if path.suffix.lower() in {".html", ".htm"}:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from extract_x_html import is_x_html, extract_file as extract_x_file
        from project_io import ProjectError
        if is_x_html(decode_text_bytes(path.read_bytes())[0]):
            try:
                return extract_x_file(path, force=force)
            except ProjectError as error:
                raise ExtractionError(str(error)) from error
    source_stat_before = path.stat()
    source_hash = sha256_file(path)
    if not force:
        reused = _reuse_if_complete(path, source_hash)
        if reused:
            return reused

    extracted = extract_source(path)
    attribution = analyze_attribution(extracted["body"])
    resources = describe_resource_directories(path) if path.suffix.lower() in {".html", ".htm"} else []
    local_resources, missing_resources = _local_reference_status(path, extracted.get("resource_references", []))
    title = extracted.get("title") or path.stem
    markdown = render_markdown(path, extracted, attribution)

    # 解析期间再次核验原件身份，避免长文件在读取中被其他程序替换后提交错配清单。
    source_stat_after = path.stat()
    if (source_stat_before.st_size, source_stat_before.st_mtime_ns) != (source_stat_after.st_size, source_stat_after.st_mtime_ns) or sha256_file(path) != source_hash:
        raise ExtractionError("提取期间原始文件发生变化；本次结果未提交，请等待下载或同步完成后重试。")

    result_dir = result_directory_for(path)
    image_candidates = []
    if path.suffix.lower() in {".html", ".htm"}:
        from extract_x_html import Tree
        from image_assets import save_candidates
        tree = Tree()
        tree.feed(decode_text_bytes(path.read_bytes())[0])
        containers = [n for n in tree.root.nodes() if n.tag == "article"]
        if not containers:
            containers = [n for n in tree.root.nodes() if n.tag == "main"]
        if containers:
            image_candidates = save_candidates(path, [n.attrs for n in containers[0].nodes() if n.tag == "img"], result_dir)
        if image_candidates:
            markdown += "\n## 原页面图片候选\n\n图片尚待内容与 OCR 复核；不把替代文字当作图中文字。\n"
            for image in image_candidates:
                if image.get("path"):
                    markdown += f"\n![原页面图片](<{image['path']}>)\n"
    body_path = result_dir / BODY_FILENAME
    metadata_path = result_dir / METADATA_FILENAME
    atomic_write_text(body_path, markdown)
    body_hash = sha256_file(body_path)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "source_file": str(path),
        "source_filename": path.name,
        "source_format": path.suffix.lower().lstrip("."),
        "original_size": source_stat_after.st_size,
        "original_sha256": source_hash,
        "title": title,
        "original_url": extracted.get("original_url"),
        "author_or_account": extracted.get("author_or_account"),
        "page_author_or_account": extracted.get("page_author_or_account"),
        "current_publisher": extracted.get("current_publisher"),
        "original_author": extracted.get("original_author"),
        "published_at": extracted.get("published_at"),
        "source_type": attribution["source_type"],
        "reposter_comment": attribution.get("reposter_comment"),
        "attribution_markers": attribution.get("attribution_markers", []),
        "attribution_notice": attribution["attribution_notice"],
        "detected_encoding": extracted.get("detected_encoding"),
        "companion_resource_directories": resources,
        "local_resource_references": local_resources,
        "missing_local_resource_references": missing_resources,
        "image_candidates": image_candidates,
        "output_sha256": {BODY_FILENAME: body_hash},
    }
    atomic_write_text(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return {
        "source": str(path),
        "result_directory": str(result_dir),
        "status": "created" if not force else "rebuilt",
        "source_type": attribution["source_type"],
        "title": title,
    }


def _is_companion_directory(directory: Path) -> bool:
    """仅在同级确有同 stem HTML 时，才把 *_files/.files 当成网页资源目录跳过。"""

    name = directory.name
    if name.endswith("_files"):
        stem = name[: -len("_files")]
    elif name.endswith(".files"):
        stem = name[: -len(".files")]
    else:
        return False
    return any(
        sibling.is_file() and sibling.stem.lower() == stem.lower() and sibling.suffix.lower() in {".html", ".htm"}
        for sibling in directory.parent.iterdir()
    )


def discover_sources(directory: Path, *, recursive: bool = True) -> list[Path]:
    """稳定发现批处理来源，跳过生成结果、Git/缓存及 HTML 伴随资源目录。"""

    found: list[Path] = []
    if recursive:
        for root, dirnames, filenames in os.walk(directory, followlinks=False):
            root_path = Path(root)
            kept: list[str] = []
            for name in dirnames:
                child = root_path / name
                if child.is_symlink() or name.endswith(RESULT_SUFFIX) or name in {".git", "__pycache__", "参考资料", "00_阶段交接", "01_转写分段"} or _is_companion_directory(child):
                    continue
                kept.append(name)
            dirnames[:] = kept
            for name in filenames:
                candidate = root_path / name
                if not candidate.is_symlink() and candidate.suffix.lower() in SUPPORTED_SUFFIXES:
                    found.append(candidate.resolve())
    else:
        found = [
            item.resolve() for item in directory.iterdir()
            if item.is_file() and not item.is_symlink() and item.suffix.lower() in SUPPORTED_SUFFIXES
        ]
    return sorted(found, key=lambda item: str(item).lower())


def _check_result_collisions(sources: Sequence[Path]) -> None:
    """阻止同目录同 stem 的不同格式静默写入同一个结果目录。"""

    destinations: dict[str, Path] = {}
    for source in sources:
        result = result_directory_for(source)
        key = os.path.normcase(str(result.resolve()))
        previous = destinations.get(key)
        if previous and previous != source:
            raise ExtractionError(
                f"批处理中 {previous.name} 与 {source.name} 会写入同一结果目录 {result.name}。"
                "请将两个来源移入不同目录或分别改名后重试。"
            )
        destinations[key] = source


def process_input(input_path: Path, *, force: bool = False, recursive: bool = True) -> dict[str, Any]:
    """处理单文件或目录；批量模式会继续其他来源并汇总每项错误。"""

    input_path = input_path.expanduser().resolve()
    if input_path.is_file():
        sources = [input_path]
    elif input_path.is_dir():
        sources = discover_sources(input_path, recursive=recursive)
        if not sources:
            raise ExtractionError("输入目录中没有找到支持的文字资料文件。")
    else:
        raise ExtractionError(f"输入路径不存在：{input_path}")
    _check_result_collisions(sources)

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for source in sources:
        try:
            results.append(extract_file(source, force=force))
        except ExtractionError as exc:
            errors.append({"source": str(source), "error": str(exc), "error_type": type(exc).__name__})
        except OSError as exc:
            errors.append({"source": str(source), "error": f"文件系统错误：{exc}", "error_type": "OSError"})
    return {
        "input": str(input_path),
        "source_count": len(sources),
        "success_count": len(results),
        "failure_count": len(errors),
        "results": results,
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器，便于单元测试复用。"""

    parser = argparse.ArgumentParser(
        description="提取本地 HTML/MHTML/TXT/MD/DOCX/PDF，并在每个原件旁生成独立 00/01 成果。"
    )
    parser.add_argument("input", type=Path, help="一个支持的文字文件，或包含多个来源的目录")
    parser.add_argument("--force", action="store_true", help="明确重建已有机械提取成果；不会修改原始来源")
    parser.add_argument("--no-recursive", action="store_true", help="目录批处理时只查看直接子文件")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出批处理汇总")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口；任一来源失败时返回非零退出码，但批量任务仍处理其余来源。"""

    args = build_parser().parse_args(argv)
    try:
        summary = process_input(args.input, force=args.force, recursive=not args.no_recursive)
    except ExtractionError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for result in summary["results"]:
            action = "复用" if result["status"] == "reused" else "生成"
            print(f"{action}：{result['source']} -> {result['result_directory']}")
        for error in summary["errors"]:
            print(f"失败：{error['source']}\n  {error['error']}", file=sys.stderr)
        print(
            f"完成：成功 {summary['success_count']} 项，失败 {summary['failure_count']} 项，"
            f"共发现 {summary['source_count']} 个来源。"
        )
    return 1 if summary["failure_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
