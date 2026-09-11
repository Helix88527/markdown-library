"""Register supplied evidence or download a selected source into 参考资料."""
from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from project_io import ProjectError, atomic_bytes, dependency_files, digest, ordinary_path, write_json

REGISTRY = '00_参考资料登记.json'


def safe_title(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')[:80] or '资料'


def load_registry(project: Path) -> dict:
    path = project / '参考资料' / REGISTRY
    if path.exists():
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if value.get('schema_version') != 'references-1':
            raise ProjectError('参考资料登记结构不兼容。')
        return value
    return {'schema_version': 'references-1', 'items': []}


def render_index(project: Path, registry: dict) -> None:
    def cell(value):
        return str(value or '未提供').replace('|', '\\|').replace('\n', ' ')
    lines = ['# 参考资料目录', '', '| 编号 | 标题与发布者 | 发布日期 | 访问日期 | 原始链接 | 本地文件 | 用途与保存状态 |',
             '|---|---|---|---|---|---|---|']
    for item in registry['items']:
        files = '；'.join(f'[文件{n}](<{path}>)' for n, path in enumerate(item['files'], 1))
        url = item.get('url')
        link = f'[原始页面](<{url}>)' if url else '未提供'
        lines.append(f"| {item['id']} | {cell(item['title'])} / {cell(item.get('publisher'))} | {cell(item.get('published_at'))} | {cell(item['accessed_at'])} | {link} | {files} | {cell(item['purpose'])}；{cell(item['save_status'])} |")
    atomic_bytes(project / '参考资料' / '参考资料目录.md', ('\n'.join(lines) + '\n').encode('utf-8'), overwrite=True)


def register(project: Path, source: Path | None, *, title: str, purpose: str, url: str = '',
             publisher: str = '', published_at: str = '', origin: str = '用户提供',
             save_status: str = '已保存原件，尚待内容核对', note: str = '') -> dict:
    project = project.resolve()
    refs = ordinary_path(project / '参考资料', project)
    registry = load_registry(project)
    if not title.strip() or not purpose.strip():
        raise ProjectError('参考资料必须有标题及具体用途。')
    if origin not in {'用户提供', '联网补充'}:
        raise ProjectError('资料来源类别无效。')
    if source is None and not note.strip():
        raise ProjectError('未保存原件时必须说明访问结果和证据缺口。')
    source_hash = digest(source) if source else None
    for item in registry['items']:
        if source_hash and item.get('source_sha256') == source_hash and item.get('url', '') == url:
            for filename, expected in item['sha256'].items():
                existing = ordinary_path(refs / filename, refs)
                if not existing.is_file() or digest(existing) != expected:
                    raise ProjectError('已登记参考资料已变化，请核对后登记新修订。')
            return {'status': 'reused', 'id': item['id'], 'index': str(refs / '参考资料目录.md')}
    identifier = f"R{len(registry['items']) + 1:03d}"
    folder = ordinary_path(refs / origin / f'{identifier}_{safe_title(title)}', project)
    files = []
    if source:
        source = source.resolve()
        if not source.is_file():
            raise ProjectError('参考原件不存在。')
        pairs = [(source, folder / source.name)]
        if source.suffix.lower() == '.md':
            pairs = [(item, folder / item.relative_to(source.parent)) for item in dependency_files(source)]
        if source.suffix.lower() in {'.html', '.htm'}:
            for name in (source.stem + '_files', source.stem + '.files', source.name + '_files', source.name + '.files'):
                companion = source.parent / name
                if companion.exists():
                    ordinary_path(companion, source.parent)
                    for item in companion.rglob('*'):
                        ordinary_path(item, source.parent)
                        if item.is_file():
                            pairs.append((item, folder / item.relative_to(source.parent)))
        for original, target in pairs:
            atomic_bytes(target, original.read_bytes())
            files.append(target.relative_to(refs).as_posix())
    else:
        target = folder / '访问记录.md'
        atomic_bytes(target, f'# {title}\n\n原始链接：{url}\n\n{note}\n'.encode('utf-8'))
        files.append(target.relative_to(refs).as_posix())
        save_status = '仅访问记录，未取得原件'
    record = {'id': identifier, 'title': title, 'publisher': publisher, 'published_at': published_at,
              'accessed_at': datetime.now(timezone.utc).isoformat(), 'url': url, 'purpose': purpose,
              'origin': origin, 'save_status': save_status, 'source_sha256': source_hash, 'files': files,
              'sha256': {f: digest(refs / f) for f in files}}
    registry['items'].append(record)
    write_json(refs / REGISTRY, registry, overwrite=True)
    render_index(project, registry)
    return {'status': 'registered', 'id': identifier, 'index': str(refs / '参考资料目录.md'), 'file_count': len(files)}


def download(project: Path, url: str, *, title: str, purpose: str, publisher: str = '', published_at: str = '', max_bytes: int = 50 * 1024 * 1024) -> dict:
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ProjectError('联网资料只接受无账号凭据的 HTTPS URL。')
    request = Request(url, headers={'User-Agent': 'MarkdownLibrary/1.0'})
    with urlopen(request, timeout=30) as response:
        final_url = response.geturl()
        if urlsplit(final_url).scheme != 'https':
            raise ProjectError('下载重定向离开 HTTPS，未保存。')
        media_type = response.headers.get_content_type()
        suffixes = {'application/pdf': '.pdf', 'text/html': '.html', 'text/plain': '.txt',
                    'application/xhtml+xml': '.html', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx'}
        if media_type not in suffixes:
            raise ProjectError(f'不支持的参考资料类型：{media_type}')
        data = response.read(max_bytes + 1)
        if not data or len(data) > max_bytes:
            raise ProjectError('下载内容为空或超过大小上限。')
    # Download into a task-local temporary file, then register as evidence.
    import tempfile
    project.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.reference-', dir=project) as temporary:
        file = Path(temporary) / (safe_title(title) + suffixes[media_type])
        file.write_bytes(data)
        return register(project, file, title=title, purpose=purpose, url=url, publisher=publisher,
                        published_at=published_at, origin='联网补充',
                        save_status=f'已下载，正文与登录页检查待复核；最终地址 {final_url}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['register', 'download', 'index'])
    parser.add_argument('project', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--title', default='')
    parser.add_argument('--purpose', default='')
    parser.add_argument('--url', default='')
    parser.add_argument('--publisher', default='')
    parser.add_argument('--published-at', default='')
    parser.add_argument('--note', default='')
    args = parser.parse_args()
    try:
        if args.command == 'index':
            render_index(args.project, load_registry(args.project))
            result = {'status': 'indexed'}
        elif args.command == 'download':
            result = download(args.project, args.url, title=args.title, purpose=args.purpose,
                              publisher=args.publisher, published_at=args.published_at)
        else:
            result = register(args.project, args.source, title=args.title, purpose=args.purpose, url=args.url,
                              publisher=args.publisher, published_at=args.published_at, note=args.note)
        print(json.dumps(result, ensure_ascii=False))
    except (ProjectError, OSError, ValueError) as error:
        parser.exit(2, f'错误：{error}\n')


if __name__ == '__main__':
    main()
