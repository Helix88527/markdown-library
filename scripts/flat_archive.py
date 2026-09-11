"""Place only the final Markdown in cooked; keep linked evidence at its source."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from project_io import ProjectError, digest, ordinary_path


def link_spans(text: str):
    """Yield destination spans, leaving fenced/inline code and titles untouched."""
    masked = re.sub(r'(?ms)^\s*(`{3,}|~{3,}).*?^\s*\1\s*$', lambda m: ' ' * len(m[0]), text)
    masked = re.sub(r'(`+)[^\n]*?\1', lambda m: ' ' * len(m[0]), masked)
    spans = []
    for match in re.finditer(r'!?\[[^\]\n]*\]\(\s*', masked):
        start = match.end()
        angle = start < len(masked) and masked[start] == '<'
        if angle:
            start += 1
        end, depth = start, 0
        while end < len(masked):
            char = masked[end]
            if char == '\\' and end + 1 < len(masked):
                end += 2
                continue
            if angle and char == '>':
                break
            if not angle:
                if char == '(':
                    depth += 1
                elif char == ')':
                    if not depth:
                        break
                    depth -= 1
                elif char.isspace():
                    break
            end += 1
        if end > start:
            spans.append((start, end))
    for pattern in (r'(?m)^\s*\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))',
                    r'(?:src|href)\s*=\s*["\x27]([^"\x27]+)["\x27]'):
        for match in re.finditer(pattern, masked, re.I):
            group = next(i for i in range(1, len(match.groups()) + 1) if match.group(i) is not None)
            spans.append(match.span(group))
    return sorted(set(spans))


def allowed_path(path: Path, roots: tuple[Path, ...]) -> Path:
    absolute = Path(os.path.abspath(path))
    for root in roots:
        if absolute.is_relative_to(root):
            return ordinary_path(absolute, root)
    raise ProjectError(f'文件引用越出资料库或源项目：{path}')


def target(raw: str, directory: Path, roots: tuple[Path, ...]):
    value = re.sub(r'\\([()\[\]<> #?])', r'\1', raw)
    if value.startswith('#'):
        return None
    # Windows drive prefixes are paths, not URL protocols.
    drive = bool(re.match(r'^[A-Za-z]:[/\\]', value))
    parsed = urlsplit(value)
    if not drive and (parsed.scheme.lower() in {'http', 'https', 'mailto', 'data', 'tel'} or parsed.netloc):
        return None
    if parsed.scheme and not drive:
        raise ProjectError(f'不支持的本地链接格式：{value}')
    path_part = parsed.scheme + ':' + parsed.path if drive else parsed.path
    suffix = ('?' + parsed.query if parsed.query else '') + ('#' + parsed.fragment if parsed.fragment else '')
    file = allowed_path(directory / unquote(path_part), roots)
    if not file.is_file():
        raise ProjectError(f'引用文件缺失：{file}')
    return file, suffix


def plan(entry: Path, destination: Path, *, database_root: Path) -> dict:
    database_root = database_root.resolve()
    source_root = entry.parent.resolve()
    roots = (database_root,) if source_root.is_relative_to(database_root) else (database_root, source_root)
    entry = allowed_path(entry, roots)
    source_bytes = entry.read_bytes()
    text = source_bytes.decode('utf-8-sig')
    pending, found = [entry], {}
    while pending:
        file = allowed_path(pending.pop(), roots)
        if file in found:
            continue
        if not file.is_file():
            raise ProjectError(f'引用文件缺失：{file}')
        found[file] = digest(file)
        if file.suffix.lower() == '.md':
            body = file.read_text(encoding='utf-8-sig')
            for begin, end in link_spans(body):
                result = target(body[begin:end], file.parent, roots)
                if result:
                    pending.append(result[0])
    replacements = []
    for begin, end in link_spans(text):
        result = target(text[begin:end], entry.parent, roots)
        if result:
            file, suffix = result
            try:
                relative = Path(os.path.relpath(file, destination.parent)).as_posix()
            except ValueError:  # Different Windows drive; original files stay put.
                relative = file.as_posix()
            replacements.append((begin, end, quote(relative, safe='/:-._~') + suffix))
    for begin, end, value in reversed(replacements):
        text = text[:begin] + value + text[end:]
    prefix = b'\xef\xbb\xbf' if source_bytes.startswith(b'\xef\xbb\xbf') else b''
    return {'data': prefix + text.encode('utf-8'), 'source_sha256': found[entry],
            'dependencies': [{'path': str(f), 'sha256': h} for f, h in sorted(found.items(), key=lambda pair: str(pair[0])) if f != entry]}


def verify_flat(entry: Path, destination: Path, *, database_root: Path) -> dict:
    expected = plan(entry, destination, database_root=database_root)
    destination = ordinary_path(destination, database_root)
    if not destination.is_file() or destination.read_bytes() != expected['data']:
        raise ProjectError('成稿稿与当前正文重定位后的内容不一致，保留原稿，不覆盖。')
    return {'status': 'verified', 'archive_format': 'flat-1', 'destination': str(destination),
            'sha256': digest(destination), 'source_sha256': expected['source_sha256'],
            'file_count': 1, 'dependency_count': len(expected['dependencies']),
            'dependencies': expected['dependencies']}


def archive(entry: Path, cooked: Path, *, database_root: Path, archive_name: str | None = None) -> dict:
    name = archive_name or entry.name
    if name in {'.', '..'} or Path(name).name != name or '/' in name or '\\' in name:
        raise ProjectError('归档名称只能是文件名或 stem。')
    if not name.lower().endswith('.md'):
        name += '.md'
    cooked = ordinary_path(cooked, database_root)
    destination = ordinary_path(cooked / name, database_root)
    expected = plan(entry, destination, database_root=database_root)
    if destination.exists():
        result = verify_flat(entry, destination, database_root=database_root)
        result['status'] = 'unchanged'
        return result
    cooked.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=cooked, prefix='.mdlib-flat-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(expected['data'])
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.read_bytes() != expected['data']:
            raise ProjectError('临时稿核对失败，未提交。')
        # Recheck the actual sources before publishing, including nested evidence.
        current = plan(entry, destination, database_root=database_root)
        if current != expected:
            raise ProjectError('归档期间正文或附件发生变化，未提交。')
        # A hard link publishes a fully written file without overwriting a racing writer.
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise ProjectError('归档目标在提交期间出现，未覆盖。') from error
        result = verify_flat(entry, destination, database_root=database_root)
        result['status'] = 'copied'
        return result
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
