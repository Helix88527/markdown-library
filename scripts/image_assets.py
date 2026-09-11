"""Materialize local or embedded source images; never render invented evidence."""
import base64
import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from project_io import ProjectError, atomic_bytes, ordinary_path


def save_candidates(source: Path, images: list[dict], output: Path) -> list[dict]:
    records = []
    for position, image in enumerate(images, 1):
        src = image.get('src', '')
        record = {'alt': image.get('alt', ''), 'status': 'pending_visual_review'}
        if src.startswith(('http://', 'https://', '//', 'blob:')):
            records.append({**record, 'source': src, 'status': 'remote_or_blob_not_saved'})
            continue
        if src.startswith('data:'):
            match = re.fullmatch(r'data:image/(png|jpeg|jpg|webp|gif);base64,(.+)', src, re.S | re.I)
            if not match:
                records.append({**record, 'source': 'embedded_image', 'status': 'unsupported_embedded_format'})
                continue
            payload = base64.b64decode(match.group(2), validate=True)
            suffix = '.' + ('jpg' if match.group(1).lower() == 'jpeg' else match.group(1).lower())
            origin = 'embedded_image'
        else:
            parsed = urlsplit(src)
            if parsed.scheme or not parsed.path:
                records.append({**record, 'source': src, 'status': 'unsupported_or_empty_source'})
                continue
            local = ordinary_path(source.parent / unquote(parsed.path), source.parent)
            if not local.is_file():
                records.append({**record, 'source': src, 'status': 'missing_local_image'})
                continue
            suffix = local.suffix.lower()
            if suffix not in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}:
                records.append({**record, 'source': src, 'status': 'unsupported_image_format'})
                continue
            payload = local.read_bytes()
            origin = src
        if not payload or len(payload) > 40 * 1024 * 1024:
            raise ProjectError('图片为空或超过 40 MiB，需单独检查。')
        sha = hashlib.sha256(payload).hexdigest()
        relative = Path('03_图片候选') / f'image-{position:03d}-{sha[:12]}{suffix}'
        atomic_bytes(output / relative, payload)
        records.append({**record, 'source': origin, 'path': relative.as_posix(), 'sha256': sha})
    return records
