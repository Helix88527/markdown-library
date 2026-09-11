"""Archive a record and its local dependencies in one atomic project folder."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from pathlib import Path

from project_io import ProjectError, dependency_files, digest, ordinary_path, write_json

MANIFEST = '00_归档清单.json'


def inventory(entry: Path) -> dict:
    files = dependency_files(entry)
    return {'schema_version': 'bundle-1', 'entrypoint': entry.name,
            'files': [{'path': f.relative_to(entry.parent.resolve()).as_posix(), 'sha256': digest(f), 'size_bytes': f.stat().st_size} for f in files]}


def verify_bundle(entry: Path, destination: Path) -> dict:
    expected = inventory(entry)
    manifest = destination / MANIFEST
    if not manifest.is_file() or json.loads(manifest.read_text(encoding='utf-8-sig')) != expected:
        raise ProjectError('归档清单与当前项目不同，可能有修订；保留两个版本，不自动覆盖。')
    for item in expected['files']:
        target = ordinary_path(destination / item['path'], destination)
        if not target.is_file() or digest(target) != item['sha256']:
            raise ProjectError(f'归档附件缺失或已被修改：{target}')
    return {'status': 'verified', 'destination': str(destination / entry.name),
            'sha256': digest(entry), 'file_count': len(expected['files'])}


def archive(entry: Path, cooked: Path, *, archive_name: str | None = None) -> dict:
    entry = entry.resolve()
    cooked = cooked.resolve()
    name = archive_name or entry.stem
    if name in {'.', '..'} or Path(name).name != name or '/' in name or '\\' in name:
        raise ProjectError('归档名称只能是一个目录名。')
    destination = ordinary_path(cooked / name, cooked)
    if destination == entry.parent or destination.is_relative_to(entry.parent):
        raise ProjectError('归档目录不能位于本次来源项目内。')
    plan = inventory(entry)
    if destination.exists():
        result = verify_bundle(entry, destination)
        result['status'] = 'unchanged'
        return result
    cooked.mkdir(parents=True, exist_ok=True)
    temporary = cooked / f'.{name}.partial.{uuid.uuid4().hex}'
    temporary.mkdir()
    owned = []
    try:
        for item in plan['files']:
            source = ordinary_path(entry.parent / item['path'], entry.parent)
            target = temporary / item['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            owned.append(target)
            if digest(target) != item['sha256']:
                raise ProjectError(f'复制期间来源变化：{source}')
        write_json(temporary / MANIFEST, plan)
        owned.append(temporary / MANIFEST)
        verify_bundle(entry, temporary)
        # Existing user archives are never renamed, replaced, or deleted.
        if destination.exists():
            raise ProjectError('归档目标在提交期间出现，未覆盖。')
        os.rename(temporary, destination)
        result = verify_bundle(entry, destination)
        result['status'] = 'copied'
        return result
    finally:
        if temporary.exists():
            for path in owned:
                ordinary_path(path, temporary).unlink(missing_ok=True)
            for directory in sorted((p for p in temporary.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                directory.rmdir()
            temporary.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('entry', type=Path)
    parser.add_argument('cooked', type=Path)
    parser.add_argument('--archive-name')
    parser.add_argument('--confirmed-by-user', action='store_true', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(archive(args.entry, args.cooked, archive_name=args.archive_name), ensure_ascii=False))
    except (ProjectError, OSError, ValueError) as error:
        parser.exit(2, f'错误：{error}\n')


if __name__ == '__main__':
    main()
