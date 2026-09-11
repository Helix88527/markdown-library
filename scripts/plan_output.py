"""只读规划资料资料任务的工作目录与最终 Markdown 备份边界。

根据当前任务的入库要求规划位置；发现资料库不代表获得写入授权。
本脚本只读，不创建目录或复制成果。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT_NAME = "Markdown资料库"
DATABASE_MARKER = ".markdown-library-database.json"
RAW_LIBRARY_DIRNAME = "原始资料"


class OutputPlanError(RuntimeError):
    """表示来源或输出位置无法安全解析。"""


def path_within(path: Path, parent: Path) -> bool:
    """判断解析后的路径是否位于指定目录内。"""

    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def find_database_root(path: Path) -> Path | None:
    """从来源向上只读寻找资料库标记。"""

    current = path.parent if path.is_file() else path
    for candidate in (current, *current.parents):
        if candidate.name == ROOT_NAME or (candidate / DATABASE_MARKER).is_file():
            return candidate.resolve()
    return None


def validate_database_root(path: Path) -> Path:
    """验证用户显式给出的资料库根目录，不创建任何标记。"""

    root = path.expanduser().resolve()
    if not root.is_dir() or not (
        root.name == ROOT_NAME or (root / DATABASE_MARKER).is_file()
    ):
        raise OutputPlanError(f"不是可识别的‘{ROOT_NAME}’根目录：{root}")
    return root


def resolve_existing_source(value: Path) -> Path:
    """解析必须已经存在的来源文件或目录。"""

    source = value.expanduser().resolve()
    if not source.exists() or not (source.is_file() or source.is_dir()):
        raise OutputPlanError(f"找不到来源文件或目录：{source}")
    return source


def build_output_plan(
    source_value: Path,
    *,
    output_dir: Path | None = None,
    database_root: Path | None = None,
    ingest_confirmed: bool = False,
) -> dict[str, Any]:
    """建立只读输出计划；不创建输出目录，也不复制成果。"""

    source = resolve_existing_source(source_value)
    detected_root = find_database_root(source)
    root = validate_database_root(database_root) if database_root else detected_root

    if output_dir is None:
        project_dir = source.parent if source.is_file() else source
        location_basis = "source_parent" if source.is_file() else "source_directory"
        explicitly_selected = False
    else:
        project_dir = output_dir.expanduser().resolve()
        location_basis = "explicit_output_dir"
        explicitly_selected = True
        if project_dir.exists() and not project_dir.is_dir():
            raise OutputPlanError(f"输出位置存在但不是目录：{project_dir}")

    source_inside_database = root is not None and path_within(source, root)
    source_inside_raw_library = bool(
        root is not None and path_within(source, root / RAW_LIBRARY_DIRNAME)
    )
    output_inside_database = root is not None and path_within(project_dir, root)
    automatic_backup_required = bool(ingest_confirmed)
    automatic_backup_ready = root is not None
    standing_ingest_authorized = False
    ingest_allowed = bool(ingest_confirmed and automatic_backup_ready)
    if ingest_allowed:
        ingest_authorization_basis = "explicit_task_request"
        ingest_note = "按当前任务要求归档最终 Markdown；保留项目原稿与附件并重定位链接。"
    elif automatic_backup_required:
        ingest_authorization_basis = "database_unavailable"
        ingest_note = "当前任务要求入库，但尚未定位目标资料库；先保留项目成果。"
    else:
        ingest_authorization_basis = "not_requested"
        ingest_note = "当前未请求入库，成果保留在项目目录。"
    return {
        "operation": "plan-output",
        "read_only": True,
        "writes_performed": False,
        "source": str(source),
        "source_kind": "file" if source.is_file() else "directory",
        "project_dir": str(project_dir),
        "project_dir_exists": project_dir.is_dir(),
        "location_basis": location_basis,
        "output_explicitly_selected": explicitly_selected,
        "database_root": str(root) if root else None,
        "source_inside_database": source_inside_database,
        "source_inside_raw_library": source_inside_raw_library,
        "output_inside_database": output_inside_database,
        "ingest_confirmed": bool(ingest_confirmed),
        "automatic_backup_required": automatic_backup_required,
        "automatic_backup_ready": automatic_backup_ready,
        "standing_ingest_authorized": standing_ingest_authorized,
        "ingest_authorization_basis": ingest_authorization_basis,
        "ingest_allowed": ingest_allowed,
        "ingest_note": ingest_note,
        "archive_format": "flat-1",
        "archive_scope": "final_markdown_only_links_to_source",
    }


def build_parser() -> argparse.ArgumentParser:
    """构造只读规划命令。"""

    parser = argparse.ArgumentParser(
        description="只读规划资料资料任务的来源、工作目录和入库授权边界。"
    )
    parser.add_argument("source", type=Path, help="待处理文件或项目目录")
    parser.add_argument("--output-dir", type=Path, help="用户明确指定的成果目录")
    parser.add_argument("--database-root", type=Path, help="可选资料库根目录，仅用于识别边界")
    parser.add_argument(
        "--ingest-confirmed",
        action="store_true",
        help="只记录用户已明确要求入库；本命令仍不复制任何文件",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。"""

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        result = build_output_plan(
            args.source,
            output_dir=args.output_dir,
            database_root=args.database_root,
            ingest_confirmed=args.ingest_confirmed,
        )
    except OutputPlanError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"来源：{result['source']}")
        print(f"项目目录：{result['project_dir']}（{result['location_basis']}）")
        print(result["ingest_note"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
