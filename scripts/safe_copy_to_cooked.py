"""在已获授权后，把最终 Markdown 或 Word 附件复制到Markdown。

资料库旧版 ``copy_to_cooked.py`` 会直接写目标文件；若替换过程中断，可能留下
截断的新文件并损坏原来的好稿。本脚本先在成稿目录写临时副本、校验哈希，再用
``os.replace`` 一次提交。即使中断，旧目标也保持完整。CLI 必须携带确认参数；
仅在当前任务已要求相应文件入库时执行；技能本身不授予写入权限。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any


ROOT_NAME = "Markdown资料库"
MARKER = ".markdown-library-database.json"
CONFIG_RELATIVE = Path("工具") / "资料整理工具" / "config.json"
ALLOWED_ARTIFACT_SUFFIXES = {".md", ".docx"}


class CopyError(RuntimeError):
    """表示路径、冲突或完整性校验错误。"""


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_root(path: Path) -> Path | None:
    """从来源向上定位可移动资料库根目录。"""

    current = path.parent if path.is_file() else path
    for candidate in (current, *current.parents):
        if candidate.name == ROOT_NAME or (candidate / MARKER).is_file():
            return candidate.resolve()
    return None


def resolve_source(value: Path, explicit_root: Path | None) -> tuple[Path, Path]:
    """解析绝对或相对成果路径，并单独定位目标资料库。

    Markdown 仍是主知识库成果；DOCX 仅用于已经由用户确认的视频视觉附件。
    两类文件共用同一套格式、冲突保护和哈希提交逻辑。显式给出资料库根目录
    时，允许从下载目录等资料库外项目复制已经完成的成果；目标仍严格限制在
    资料库的 cooked_dir 内。CLI 的 --confirmed-by-user 门表示调用方确认当前
    当前任务已要求对应 Markdown 或 Word 文件入库。
    """

    if explicit_root:
        root = explicit_root.expanduser().resolve()
    elif value.is_absolute():
        root = find_root(value.expanduser().resolve())
        if root is None:
            raise CopyError("无法从来源找到“Markdown资料库”；请添加 --database-root。")
    else:
        root = find_root(Path.cwd())
        if root is None:
            raise CopyError("相对路径需要 --database-root，或从资料库内部运行。")
    config_path = root / CONFIG_RELATIVE
    if not config_path.is_file():
        raise CopyError(f"资料库缺少工具包配置：{config_path}")
    raw = value.expanduser()
    if raw.is_absolute():
        source = raw.resolve()
    else:
        parts = raw.parts
        if parts and parts[0] == ROOT_NAME:
            raw = Path(*parts[1:]) if len(parts) > 1 else Path(".")
        source = (root / raw).resolve()
    if (
        not source.is_file()
        or source.suffix.lower() not in ALLOWED_ARTIFACT_SUFFIXES
        or source.stat().st_size == 0
    ):
        allowed = "、".join(sorted(ALLOWED_ARTIFACT_SUFFIXES))
        raise CopyError(f"输入必须是存在且非空的成果文件；允许格式：{allowed}。")
    return source, root


def cooked_directory(root: Path) -> Path:
    """从工具包配置读取Markdown目录。"""

    config = json.loads((root / CONFIG_RELATIVE).read_text(encoding="utf-8-sig"))
    raw = Path(config["cooked_dir"]).expanduser()
    destination = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    try:
        destination.relative_to(root)
    except ValueError as error:
        raise CopyError(f"成稿目录越出资料库：{destination}") from error
    return destination


def copy_atomic(
    source: Path,
    destination: Path,
    *,
    replace: bool,
    replace_confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """复制到同目录临时文件，校验后原子替换目标。"""

    if replace and not replace_confirmed_by_user:
        raise CopyError(
            "--replace 需要用户对本次覆盖的独立明确确认；"
            "常规入库授权不能替代 --replace-confirmed-by-user。"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_hash = sha256_file(source)
    if destination.exists():
        destination_hash = sha256_file(destination)
        if destination_hash == source_hash:
            return {
                "status": "unchanged",
                "source": str(source),
                "destination": str(destination),
                "sha256": source_hash,
            }
        if not replace:
            raise CopyError(
                "Markdown目标存在且内容不同，可能包含用户手工修订；自动入库不得覆盖。"
                "请先比较当前成稿与新稿，并在用户明确确认替换后才添加 --replace。"
            )

    temporary = destination.with_name(f".{destination.name}.partial.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        shutil.copy2(source, temporary)
        temporary_hash = sha256_file(temporary)
        if temporary_hash != source_hash:
            raise CopyError("临时副本 SHA256 与来源不一致，未替换成稿目标。")
        os.replace(temporary, destination)
        final_hash = sha256_file(destination)
        if final_hash != source_hash:
            raise CopyError("成稿目标提交后的 SHA256 校验失败。")
        return {
            "status": "copied",
            "source": str(source),
            "destination": str(destination),
            "sha256": final_hash,
        }
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数。"""

    parser = argparse.ArgumentParser(description="经用户明确授权后，把最终 Markdown 或 Word 视觉附件原子复制到资料Markdown。")
    parser.add_argument("artifact", type=Path, help="资料库内外均可使用的 .md 或 .docx 成果")
    parser.add_argument("--database-root", type=Path)
    parser.add_argument("--archive-name", help="平铺 Markdown 文件名或 stem；修订版使用新文件名")
    parser.add_argument(
        "--confirmed-by-user",
        action="store_true",
        required=True,
        help="确认已获授权（当前任务对 Markdown 或 Word 的入库要求）",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="仅在比较当前成稿并得到用户对本次替换的明确确认后，允许原子替换同一来源旧稿",
    )
    parser.add_argument(
        "--replace-confirmed-by-user",
        action="store_true",
        help="确认用户已查看差异并明确授权本次覆盖；必须与 --replace 同时使用",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读的目标和哈希")
    return parser


def main() -> None:
    """命令行入口。"""

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        source, root = resolve_source(args.artifact, args.database_root)
        if args.replace_confirmed_by_user and not args.replace:
            raise CopyError("--replace-confirmed-by-user 只能与 --replace 同时使用。")
        if source.suffix.lower() == ".md":
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from flat_archive import archive
            from project_io import ProjectError
            if args.replace:
                raise CopyError("平铺归档不自动覆盖已有稿件；请比较后用新的标题或 --archive-name 保留修订版。")
            try:
                result = archive(source, cooked_directory(root), database_root=root, archive_name=args.archive_name)
            except ProjectError as error:
                raise CopyError(str(error)) from error
        else:
            result = copy_atomic(
                source, cooked_directory(root) / source.name, replace=args.replace,
                replace_confirmed_by_user=args.replace_confirmed_by_user,
            )
    except CopyError as error:
        parser.exit(2, f"错误：{error}\n")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["status"] == "unchanged":
        print(f"Markdown中已有相同文件：{result['destination']}")
    else:
        print(f"已经安全复制到Markdown：{result['destination']}")
        print(f"SHA256：{result['sha256']}")


if __name__ == "__main__":
    main()
