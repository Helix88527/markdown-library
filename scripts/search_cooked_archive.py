"""只读检索Markdown，为校正和增量写作提供候选往期材料。

脚本只做词项重合排序，不判断事实，也不把候选内容写入当前成果。调用方必须
实际阅读命中的章节，再决定它属于重复、补充、变化还是冲突。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT_NAME = "Markdown资料库"
MARKER = ".markdown-library-database.json"
CONFIG_RELATIVE = Path("工具") / "资料整理工具" / "config.json"
TEXT_SUFFIXES = {".md", ".txt", ".srt", ".vtt"}
LATIN_OR_NUMBER = re.compile(
    r"[a-z][a-z0-9._+\-]{1,}|(?:19|20)\d{2}|\d+(?:\.\d+)?(?:亿元|万元|万|亿|%|％|年|月|日|人|次|家|岁)",
    re.I,
)
CHINESE_RUN = re.compile(r"[\u3400-\u9fff]{2,}")
HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)
STOP_TERMS = {
    "一个", "一些", "这个", "那个", "就是", "其实", "可能", "可以", "因为", "所以",
    "但是", "然后", "还是", "已经", "没有", "什么", "这样", "里面", "现在", "他们",
    "我们", "你们", "自己", "比较", "觉得", "问题", "事情", "时候", "内容", "进行",
    "来说", "的话", "对吧", "这里", "那里", "这种", "很多", "非常", "怎么", "为什么",
}


class SearchError(RuntimeError):
    """表示查询文件或资料库边界无法安全解析。"""


def path_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def find_database_root(path: Path) -> Path | None:
    current = path.parent if path.is_file() else path
    for candidate in (current, *current.parents):
        if candidate.name == ROOT_NAME or (candidate / MARKER).is_file():
            return candidate.resolve()
    return None


def resolve_database_root(query: Path, explicit: Path | None) -> Path:
    root = explicit.expanduser().resolve() if explicit else find_database_root(query)
    if root is None or not root.is_dir() or not (
        root.name == ROOT_NAME or (root / MARKER).is_file()
    ):
        raise SearchError("无法确定‘Markdown资料库’资料库根目录；请添加 --database-root。")
    return root


def cooked_directory(root: Path) -> Path:
    config_path = root / CONFIG_RELATIVE
    if not config_path.is_file():
        raise SearchError(f"资料库缺少工具包配置：{config_path}")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        raw = Path(config["cooked_dir"]).expanduser()
    except (KeyError, json.JSONDecodeError) as error:
        raise SearchError(f"工具包配置缺少有效 cooked_dir：{config_path}") from error
    cooked = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if not path_within(cooked, root):
        raise SearchError(f"成稿目录越出资料库：{cooked}")
    if not cooked.is_dir():
        raise SearchError(f"成稿目录不存在：{cooked}")
    return cooked


def decode_text(raw: bytes, path: Path) -> str:
    """在不再次访问文件的情况下解码已经取得的一致字节快照。"""

    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            continue
    raise SearchError(f"无法识别文本编码：{path}")


def read_snapshot(path: Path, *, attempts: int = 3) -> dict[str, Any]:
    """直接读取当前文件，并返回可审计且内部一致的内容快照。

    本函数不读取缓存或索引。读取前后的大小、修改时间和文件身份必须一致，
    防止用户恰在回查期间修订成稿时把两个版本拼成一个结果。
    """

    if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
        allowed = "、".join(sorted(TEXT_SUFFIXES))
        raise SearchError(f"查询输入必须是存在的文本成果；允许格式：{allowed}。")
    last_error: OSError | None = None
    for _ in range(attempts):
        try:
            before = path.stat()
            raw = path.read_bytes()
            after = path.stat()
        except OSError as error:
            last_error = error
            continue
        if (
            before.st_dev == after.st_dev
            and before.st_ino == after.st_ino
            and before.st_size == after.st_size == len(raw)
            and before.st_mtime_ns == after.st_mtime_ns
        ):
            return {
                "text": decode_text(raw, path),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "modified_at": datetime.fromtimestamp(
                    after.st_mtime, timezone.utc
                ).isoformat().replace("+00:00", "Z"),
                "modified_at_ns": after.st_mtime_ns,
                "size_bytes": len(raw),
            }
    detail = f"：{last_error}" if last_error else ""
    raise SearchError(f"文件读取期间发生变化，未使用可能过期的内容；请重跑：{path}{detail}")


def read_text(path: Path) -> str:
    """兼容调用方：始终返回当前文件的一致文本快照。"""

    return str(read_snapshot(path)["text"])


def term_counter(text: str) -> Counter[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    terms: Counter[str] = Counter()
    for token in LATIN_OR_NUMBER.findall(normalized):
        if token not in STOP_TERMS:
            terms[token] += 1
    for run in CHINESE_RUN.findall(normalized):
        if 2 <= len(run) <= 8 and run not in STOP_TERMS:
            terms[run] += 2
        for width in (2, 3, 4):
            if len(run) < width:
                continue
            for index in range(len(run) - width + 1):
                token = run[index : index + width]
                if token not in STOP_TERMS:
                    terms[token] += 1
    return terms


def term_length_weight(term: str) -> float:
    if CHINESE_RUN.fullmatch(term):
        return {2: 0.65, 3: 1.0, 4: 1.35}.get(len(term), 1.6)
    return 1.0


def document_title(path: Path, text: str) -> str:
    match = re.search(r"^#\s+(.+)$", text, re.M)
    return match.group(1).strip() if match else path.stem


def search_archive(
    query_path: Path,
    cooked: Path,
    *,
    limit: int = 8,
    excluded: Iterable[Path] = (),
) -> dict[str, Any]:
    query = query_path.expanduser().resolve()
    query_snapshot = read_snapshot(query)
    query_text = str(query_snapshot["text"])
    query_terms = term_counter(query_text)
    if not query_terms:
        raise SearchError("查询文本没有可用于检索的有效词项。")

    excluded_paths = {path.expanduser().resolve() for path in excluded}
    excluded_paths.add(query)
    candidates: list[
        tuple[Path, dict[str, Any], Counter[str], Counter[str]]
    ] = []
    for path in sorted(cooked.rglob("*.md"), key=lambda item: str(item).casefold()):
        # A bundle contains reference Markdown too. Only its declared entrypoint
        # is a cooked record; user edits to that entrypoint are still read live.
        bundle_parent = next((p for p in path.parents if p != cooked and p.is_relative_to(cooked)
                              and (p / "00_归档清单.json").is_file()), None)
        if bundle_parent:
            manifest = json.loads((bundle_parent / "00_归档清单.json").read_text(encoding="utf-8-sig"))
            if path != bundle_parent / manifest.get("entrypoint", ""):
                continue
        resolved = path.resolve()
        if path.is_symlink() or resolved in excluded_paths:
            continue
        snapshot = read_snapshot(resolved)
        text = str(snapshot["text"])
        headings = "\n".join(HEADING.findall(text))
        title_terms = term_counter(f"{path.stem}\n{headings}")
        candidates.append((resolved, snapshot, term_counter(text), title_terms))

    document_frequency: Counter[str] = Counter()
    for _, _, body_terms, _ in candidates:
        document_frequency.update(body_terms.keys())
    total_documents = len(candidates)
    query_norm = math.sqrt(sum((1.0 + math.log(count)) ** 2 for count in query_terms.values()))
    matches: list[dict[str, Any]] = []

    for path, snapshot, body_terms, title_terms in candidates:
        text = str(snapshot["text"])
        contributions: list[tuple[str, float]] = []
        for term in query_terms.keys() & body_terms.keys():
            idf = math.log((total_documents + 1.0) / (document_frequency[term] + 1.0)) + 1.0
            frequency = (1.0 + math.log(query_terms[term])) * (1.0 + math.log(body_terms[term]))
            heading_boost = 2.5 if term in title_terms else 1.0
            value = frequency * idf * heading_boost * term_length_weight(term)
            contributions.append((term, value))
        if not contributions:
            continue
        contributions.sort(key=lambda item: (-item[1], -len(item[0]), item[0]))
        body_norm = math.sqrt(sum((1.0 + math.log(count)) ** 2 for count in body_terms.values()))
        score = sum(value for _, value in contributions) / max(query_norm * body_norm, 1.0)
        matches.append(
            {
                "path": str(path),
                "title": document_title(path, text),
                "score": round(score, 6),
                "shared_terms": [term for term, _ in contributions[:20]],
                "modified_at": snapshot["modified_at"],
                "modified_at_ns": snapshot["modified_at_ns"],
                "sha256": snapshot["sha256"],
                "size_bytes": snapshot["size_bytes"],
                "content_source": "live-cooked-file",
            }
        )

    matches.sort(key=lambda item: (-item["score"], item["path"].casefold()))
    return {
        "operation": "search-cooked-archive",
        "read_only": True,
        "writes_performed": False,
        "query": str(query),
        "query_sha256": query_snapshot["sha256"],
        "cooked_dir": str(cooked.resolve()),
        "candidate_count": total_documents,
        "match_count": len(matches),
        "matches": matches[:limit],
        "snapshot_policy": "每次直接读取成稿目录中的当前文件，不使用持久缓存、旧索引或源目录副本。",
        "usage_note": "候选只用于人工／代理复核；搜索命中不等于内容相同或事实已获证实。",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读检索成稿 Markdown，为校正和增量写作提供候选。")
    parser.add_argument("query", type=Path, help="01／02 逐字稿或当前正文")
    parser.add_argument("--database-root", type=Path)
    parser.add_argument("--exclude", type=Path, action="append", default=[])
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit < 1 or args.limit > 50:
        parser.error("--limit 必须在 1 到 50 之间。")
    query = args.query.expanduser().resolve()
    try:
        root = resolve_database_root(query, args.database_root)
        result = search_archive(
            query,
            cooked_directory(root),
            limit=args.limit,
            excluded=args.exclude,
        )
    except SearchError as error:
        parser.exit(2, f"错误：{error}\n")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for match in result["matches"]:
            terms = "、".join(match["shared_terms"][:8])
            print(
                f"{match['score']:.6f}\t{match['title']}\t{match['path']}\t"
                f"{match['modified_at']}\t{match['sha256']}\t{terms}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
