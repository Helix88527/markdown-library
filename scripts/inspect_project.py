"""Read-only classification of main material, references, and generated work."""
import argparse
import json
from pathlib import Path

VIDEO = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.flv'}
AUDIO = {'.mp3', '.wav', '.m4a', '.flac', '.aac', '.ogg', '.opus'}
TEXT = {'.html', '.htm', '.mhtml', '.mht', '.pdf', '.docx', '.txt', '.md', '.srt', '.vtt'}


def inspect(path: Path) -> dict:
    path = path.resolve()
    project = path.parent if path.is_file() else path
    if not project.is_dir():
        raise ValueError('项目不存在。')
    from extract_x_html import is_x_html
    materials, outputs = [], []
    for file in sorted(project.iterdir()):
        if not file.is_file() or file.is_symlink():
            continue
        suffix = file.suffix.lower()
        if file.name.startswith(('00_', '01_', '02_', '03_')) or file.stem.endswith('_音频'):
            outputs.append(file.name)
            continue
        if suffix not in VIDEO | AUDIO | TEXT:
            continue
        kind = 'video' if suffix in VIDEO else 'audio' if suffix in AUDIO else 'text'
        if suffix in {'.html', '.htm'}:
            from extract_article import decode_text_bytes
            kind = 'x_html' if is_x_html(decode_text_bytes(file.read_bytes())[0]) else 'article_html'
        elif suffix in {'.srt', '.vtt'}:
            kind = 'transcript'
        materials.append({'file': file.name, 'material_type': kind,
                          'role': 'explicit_input' if path.is_file() and file == path else 'candidate',
                          'size_bytes': file.stat().st_size})
    refs = project / '参考资料'
    return {'project': str(project), 'materials': materials, 'existing_outputs': outputs,
            'reference_directory': str(refs) if refs.is_dir() else None,
            'reference_policy': '参考资料只用于校正和核验；不存在时按实际证据缺口创建。',
            'requires_source_choice': len([m for m in materials if m['role'] == 'explicit_input']) == 0 and len(materials) > 1}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.input), ensure_ascii=False))
