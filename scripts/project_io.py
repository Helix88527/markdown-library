"""Small file and link helpers shared by the 3.x project tools."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit


class ProjectError(RuntimeError):
    pass


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic_bytes(path: Path, data: bytes, *, overwrite: bool = False) -> None:
    if path.exists():
        if path.read_bytes() == data:
            return
        if not overwrite:
            raise ProjectError(f'文件已存在且内容不同，保留原稿：{path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.mdlib-', suffix='.tmp', delete=False) as stream:
            name = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        name = None
    finally:
        if name:
            Path(name).unlink(missing_ok=True)


def write_json(path: Path, value: object, *, overwrite: bool = False) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'), overwrite=overwrite)


def ordinary_path(path: Path, root: Path) -> Path:
    root = root.resolve()
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise ProjectError(f'文件引用越出项目：{path}') from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ProjectError(f'不跟随链接或 junction：{current}')
    resolved = absolute.resolve()
    if not resolved.is_relative_to(root):
        raise ProjectError(f'文件引用越出项目：{path}')
    return resolved


def local_links(text: str) -> list[str]:
    """Collect inline/reference Markdown and HTML links; code examples are inert.

    Paths with spaces use <...>; balanced parentheses and escapes are supported.
    Remote links and same-document anchors are not bundle dependencies.
    """
    text = re.sub(r'(?ms)^\s*(`{3,}|~{3,}).*?^\s*\1\s*$', '', text)
    text = re.sub(r'`+[^`\n]*`+', '', text)
    values = []
    inline = []
    for match in re.finditer(r'!?\[[^\]\n]*\]\(\s*', text):
        i = match.end()
        value = ''
        depth = 0
        angle = i < len(text) and text[i] == '<'
        if angle:
            i += 1
        while i < len(text):
            char = text[i]
            if char == '\\' and i + 1 < len(text):
                value += text[i + 1]
                i += 2
                continue
            if angle and char == '>':
                break
            if not angle:
                if char == '(':
                    depth += 1
                elif char == ')':
                    if depth == 0:
                        break
                    depth -= 1
                elif char.isspace():
                    break
            value += char
            i += 1
        if value:
            inline.append(value)
    patterns = [r'(?m)^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)',
                r'(?:src|href)\s*=\s*[\"\x27]([^\"\x27]+)[\"\x27]']
    groups = [inline] + [re.findall(pattern, text, re.I) for pattern in patterns]
    for group in groups:
        for value in group:
            value = value.strip('<>')
            if value.startswith('#'):
                continue
            parsed = urlsplit(value)
            if parsed.scheme.lower() in {'http', 'https', 'mailto', 'data', 'tel'} or parsed.netloc:
                continue
            if parsed.scheme or value.startswith(('/', '\\')):
                raise ProjectError(f'归档正文必须使用项目内相对文件链接：{value}')
            relative = unquote(parsed.path)
            if relative:
                values.append(relative)
    return list(dict.fromkeys(values))


def dependency_files(entry: Path) -> list[Path]:
    root = entry.parent.resolve()
    pending = [entry.resolve()]
    found = set()
    while pending:
        path = ordinary_path(pending.pop(), root)
        if path in found:
            continue
        if not path.is_file():
            raise ProjectError(f'引用文件缺失：{path}')
        found.add(path)
        # HTML snapshots may reference their original site root. They are retained
        # as evidence; only Markdown dependencies form the portable record graph.
        if path.suffix.lower() == '.md':
            for link in local_links(path.read_text(encoding='utf-8-sig')):
                pending.append(path.parent / link)
    return sorted(found, key=str)
