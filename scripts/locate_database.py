"""定位可整体移动的“Markdown资料库”资料库。

优先从用户给出的资料库内路径向上寻找标记；其次检查环境提示、常见用户目录，
最后才有限深度扫描逻辑盘。搜索会跳过系统、缓存和依赖目录，避免无界遍历。
若出现多个同等可信候选则拒绝猜测，要求调用者明确指定。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from collections import deque
from pathlib import Path


ROOT_NAME = "Markdown资料库"
MARKER = ".markdown-library-database.json"
TOOLKIT_CONFIG = Path("工具") / "资料整理工具" / "config.json"
SKIP_NAMES = {
    "$recycle.bin",
    "system volume information",
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "appdata",
    ".git",
    ".cache",
    "node_modules",
}


def root_from(path: Path) -> Path | None:
    """从文件或目录逐级向上查找根目录名或标记文件。"""

    path = path.expanduser()
    if path.is_file():
        path = path.parent
    for candidate in (path, *path.parents):
        if candidate.name == ROOT_NAME or (candidate / MARKER).is_file():
            return candidate.resolve()
    return None


def is_database(path: Path) -> bool:
    """以目录名、标记或工具包配置判断候选是否像完整资料库。"""

    return path.is_dir() and (
        path.name == ROOT_NAME
        or (path / MARKER).is_file()
        or (path / TOOLKIT_CONFIG).is_file()
    )


def logical_drives() -> list[Path]:
    """返回当前平台可搜索的逻辑盘根；非 Windows 仅返回文件系统根。"""

    if os.name != "nt":
        return [Path("/")]
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [Path(f"{chr(65 + index)}:\\") for index in range(26) if mask & (1 << index)]


def bounded_search(start: Path, max_depth: int) -> list[Path]:
    """广度优先有限深度搜索，跳过系统目录、缓存、依赖和符号链接。"""

    if not start.is_dir():
        return []
    found: list[Path] = []
    queue: deque[tuple[Path, int]] = deque([(start, 0)])
    while queue:
        current, depth = queue.popleft()
        if is_database(current):
            found.append(current.resolve())
            continue
        direct = current / ROOT_NAME
        if direct.is_dir():
            found.append(direct.resolve())
        if depth >= max_depth:
            continue
        try:
            children = [entry for entry in current.iterdir() if entry.is_dir()]
        except (OSError, PermissionError):
            continue
        for child in children:
            if child.name.lower() in SKIP_NAMES or child.is_symlink():
                continue
            queue.append((child, depth + 1))
    return found


def score(path: Path) -> int:
    """按标记、配置和资料目录完整度给候选排序。"""

    value = 0
    if path.name == ROOT_NAME:
        value += 10
    if (path / MARKER).is_file():
        value += 100
    if (path / TOOLKIT_CONFIG).is_file():
        value += 50
    if (path / "原始资料").is_dir():
        value += 10
    if (path / "Markdown").is_dir():
        value += 10
    return value


def locate(hint: Path | None, max_depth: int) -> list[Path]:
    """按提示路径、环境、常见位置和有限全盘搜索返回候选。"""

    priority: list[Path] = []
    if hint:
        located = root_from(hint)
        if located:
            return [located]
        priority.append(hint)
    environment_hint = os.environ.get("MARKDOWN_LIBRARY_ROOT")
    if environment_hint:
        located = root_from(Path(environment_hint))
        if located:
            return [located]

    home = Path.home()
    priority.extend(
        [
            Path.cwd(),
            home / "Desktop" / ROOT_NAME,
            home / "Documents" / ROOT_NAME,
            home / "Downloads" / ROOT_NAME,
            home,
        ]
    )
    unique: dict[str, Path] = {}
    for candidate in priority:
        located = root_from(candidate)
        if located:
            unique[str(located).lower()] = located
        if is_database(candidate):
            resolved = candidate.resolve()
            unique[str(resolved).lower()] = resolved
    if unique:
        return sorted(unique.values(), key=score, reverse=True)

    for drive in logical_drives():
        for candidate in bounded_search(drive, max_depth):
            unique[str(candidate).lower()] = candidate
    return sorted(unique.values(), key=score, reverse=True)


def main() -> None:
    """命令行入口；无结果或多项并列时以清楚错误退出。"""

    parser = argparse.ArgumentParser(description="自动寻找可移动的“Markdown资料库”根目录。")
    parser.add_argument("--hint", type=Path, help="数据库内部任意文件或文件夹，可显著加快定位")
    parser.add_argument("--max-depth", type=int, default=5, help="全盘兜底搜索的最大目录深度")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    results = locate(args.hint, max(1, args.max_depth))
    if not results:
        raise SystemExit("没有找到“Markdown资料库”。请提供资料库内任意路径作为 --hint。")
    best_score = score(results[0])
    best = [path for path in results if score(path) == best_score]
    if len(best) > 1:
        listing = "\n".join(str(path) for path in best)
        raise SystemExit(f"找到多个同等可信的“Markdown资料库”，请指定其中一个作为 --hint：\n{listing}")
    result = best[0]
    if args.json:
        print(json.dumps({"database_root": str(result), "score": score(result)}, ensure_ascii=False))
    else:
        print(result)


if __name__ == "__main__":
    main()
