"""Assemble a full-text record without asking an LLM to regenerate the text."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from project_io import ProjectError, atomic_bytes, digest, write_json

BEGIN = '<!-- mdlib:corrected-fulltext:begin -->'
END = '<!-- mdlib:corrected-fulltext:end -->'
REQUIRED_SECTIONS = ('## 来源与作者关系', '## 主题要点与分析', '## 背景与概念补充',
                     '## 信息核验表', '## 重要校正与存疑项', '## 参考资料目录', '## 待核问题与处理记录')


TEXT_TYPES = {'article_html', 'x_html', 'article', 'pdf', 'docx', 'text'}


def sections_for(mode):
    if mode not in {'text_organization', 'media_transcript'}:
        raise ProjectError('未知内容处理模式。')
    return tuple(h for h in REQUIRED_SECTIONS if mode != 'text_organization' or h != '## 重要校正与存疑项')


def verify(source: Path, final: Path, report: Path, *, update_report: bool = True) -> dict:
    payload = json.loads(report.read_text(encoding='utf-8-sig'))
    if payload.get('schema_version') != 'fulltext-1':
        raise ProjectError('不是 fulltext-1 覆盖记录。')
    text = source.read_text(encoding='utf-8-sig')
    result = final.read_text(encoding='utf-8-sig')
    if digest(source) != payload['source']['sha256']:
        raise ProjectError('校正稿已修改，请重新组装并核对。')
    if (result.count(BEGIN) != 1 or result.count(END) != 1
            or BEGIN + '\n' not in result or '\n' + END not in result
            or result.index(BEGIN) >= result.index(END)):
        raise ProjectError('校正全文标记缺失或重复。')
    embedded = result.split(BEGIN + '\n', 1)[1].split('\n' + END, 1)[0]
    if embedded != text:
        raise ProjectError('最终稿中的校正全文与当前 02 不一致。')
    analysis = result.split('\n' + END, 1)[1]
    for heading in sections_for(payload.get('content_mode', 'media_transcript')):
        if heading not in analysis:
            raise ProjectError(f'缺少标准章节：{heading}')
    summary = {'status': 'passed', 'mode': 'exact_corrected_fulltext', 'source_sha256': digest(source),
               'final_sha256': digest(final), 'character_count': len(text), 'mechanical_gate_only': True,
               'semantic_review_scope': '仅检查正文完整组装；文字整理不要求逐字校订，媒体另核对识别质量；哈希不证明事实真伪'}
    if update_report:
        payload['audit'] = summary
        write_json(report, payload, overwrite=True)
    return summary


def assemble(source: Path, commentary: Path, metadata: dict, output: Path, report: Path) -> dict:
    if len({output.resolve(), source.resolve(), commentary.resolve(), report.resolve()}) != 4:
        raise ProjectError('最终稿不能覆盖校正稿或分析稿。')
    text = source.read_text(encoding='utf-8-sig')
    analysis = commentary.read_text(encoding='utf-8-sig')
    if not text.strip():
        raise ProjectError('校正全文为空。')
    if BEGIN in text + analysis or END in text + analysis:
        raise ProjectError('输入含保留标记。')
    for name in ('title', 'material_type', 'authorship', 'source_files'):
        if not metadata.get(name):
            raise ProjectError(f'资料信息缺少 {name}。')
    if metadata['material_type'] not in {'video', 'audio', 'article_html', 'x_html', 'transcript', 'article', 'pdf', 'docx', 'text'}:
        raise ProjectError('material_type 不在支持的载体类型中。')
    if metadata['authorship'] not in {'original', 'repost', 'repost_with_comment', 'mixed', 'unknown'}:
        raise ProjectError('authorship 不在支持的归属类型中。')
    if not isinstance(metadata['source_files'], list):
        raise ProjectError('source_files 必须是来源文件列表。')
    mode = metadata.get('content_mode') or ('text_organization' if metadata['material_type'] in TEXT_TYPES else 'media_transcript')
    for heading in sections_for(mode):
        if heading not in analysis:
            raise ProjectError(f'分析稿缺少标准章节：{heading}')
    info = dict(metadata)
    info['content_mode'] = mode
    if not info.get('processed_at'):
        info['processed_at'] = datetime.now(timezone.utc).isoformat()
    info['skill_version'] = (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip()
    info.setdefault('document_version', '1')
    info['corrected_sha256'] = digest(source)
    # JSON strings, lists and objects are valid YAML scalar/flow syntax.
    front = '\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}' for key, value in info.items())
    if any(not key.replace('_', '').isalnum() for key in info):
        raise ProjectError('元数据字段名只能包含字母、数字与下划线。')
    title = str(info['title']).replace('\n', ' ')
    body_heading = '正文整理' if mode == 'text_organization' else '校正后的全文'
    result = f'---\n{front}\n---\n\n# {title}\n\n## {body_heading}\n\n{BEGIN}\n{text}\n{END}\n\n{analysis.rstrip()}\n'
    # Preflight both destinations before any commit, protecting hand edits.
    raw = result.encode('utf-8')
    for path, data in ((output, raw),):
        if path.exists() and path.read_bytes() != data:
            raise ProjectError(f'成果已存在且内容不同，请另存修订文件：{path}')
    coverage = {'schema_version': 'fulltext-1', 'content_mode': mode, 'source': {'filename': source.name, 'sha256': digest(source)},
                'final': {'filename': output.name, 'sha256': hashlib.sha256(raw).hexdigest()},
                'method': '程序原样纳入校正全文；不再重写第二份完整正文'}
    if report.exists():
        old = json.loads(report.read_text(encoding='utf-8-sig'))
        if old.get('source') != coverage['source'] or old.get('final') != coverage['final']:
            raise ProjectError('已有覆盖记录属于不同稿件，请使用新的报告路径。')
    atomic_bytes(output, raw)
    write_json(report, coverage, overwrite=True)
    return verify(source, output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--analysis', type=Path)
    parser.add_argument('--metadata', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    try:
        if args.verify:
            result = verify(args.source, args.output, args.report)
        else:
            if not args.analysis or not args.metadata:
                raise ProjectError('组装需要 --analysis 和 --metadata。')
            result = assemble(args.source, args.analysis, json.loads(args.metadata.read_text(encoding='utf-8-sig')), args.output, args.report)
        print(json.dumps(result, ensure_ascii=False))
    except (ProjectError, OSError, ValueError, KeyError) as error:
        parser.exit(2, f'错误：{error}\n')


if __name__ == '__main__':
    main()
