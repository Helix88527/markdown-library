"""Extract saved X/Twitter cards without merging quoted users into the author."""
from __future__ import annotations

import argparse
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from project_io import ProjectError, atomic_bytes, digest, write_json


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def nodes(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.nodes()

    def text(self):
        if self.tag == 'img':
            return self.attrs.get('alt', '')
        return ''.join(child.text() if isinstance(child, Node) else child for child in self.children)


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag == 'br':
            node.children.append('\n')
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.current.tag == tag:
            self.current = self.current.parent

    def handle_endtag(self, tag):
        current = self.current
        while current.parent:
            if current.tag == tag:
                self.current = current.parent
                return
            current = current.parent

    def handle_data(self, data):
        self.current.children.append(data)


def is_x_html(text: str) -> bool:
    return bool(re.search(r'data-testid\s*=\s*[\"\x27]tweet[\"\x27]', text, re.I) or
                re.search(r'<meta[^>]+(?:content=[\"\x27]https?://(?:www\.)?(?:x|twitter)\.com/|(?:og:url)[^>]+https?://(?:www\.)?(?:x|twitter)\.com/)', text, re.I))


def under(node: Node, ancestor: Node) -> bool:
    while node:
        if node is ancestor:
            return True
        node = node.parent
    return False


def status_link(value: str):
    full = urljoin('https://x.com', value)
    parsed = urlsplit(full)
    if parsed.hostname not in {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'}:
        return None
    match = re.fullmatch(r'/([^/]+)/status/(\d+)(?:/.*)?', parsed.path)
    return (match.group(1), match.group(2), f'https://x.com/{match.group(1)}/status/{match.group(2)}') if match else None


def extract_posts(text: str) -> dict:
    tree = Tree()
    tree.feed(text)
    all_nodes = list(tree.root.nodes())
    cards = [n for n in all_nodes if n.attrs.get('data-testid') == 'tweet']
    if not cards:
        raise ProjectError('未找到可提取推文卡片；可能只保存了页面外壳，需要补充已展开页面或逐条存档。')
    cards = [n for n in cards if not any(under(n.parent, other) for other in cards if other is not n)]
    posts, warnings = [], ['存档范围仅限已保存 DOM；无法据此证明线程完整或代表账号全部发言。']
    seen = {}
    for card in cards:
        descendants = list(card.nodes())
        quotes = [n for n in descendants if n.attrs.get('data-testid') == 'tweet' or
                  (n.attrs.get('role') == 'link' and any(x.attrs.get('data-testid') in {'tweetText', 'tweetPhoto'} for x in n.nodes())
                   and any(x.attrs.get('data-testid') == 'User-Name' for x in n.nodes()))]
        quotes = [n for n in quotes if not any(under(n.parent, q) for q in quotes if q is not n)]

        def record(container, excluded):
            nodes = [n for n in container.nodes() if not any(under(n, q) for q in excluded)]
            times = [n for n in nodes if n.tag == 'time']
            identity, date = None, None
            for time in times:
                parent = time.parent
                while parent and parent is not container:
                    if parent.tag == 'a' and status_link(parent.attrs.get('href', '')):
                        identity = status_link(parent.attrs['href'])
                        date = time.attrs.get('datetime')
                        break
                    parent = parent.parent
                if identity:
                    break
            # Saved custom cards may explicitly carry an ID; no inference from
            # an arbitrary link mentioned inside the tweet text is allowed.
            post_id = identity[1] if identity else container.attrs.get('data-tweet-id')
            names = [n.text().strip() for n in nodes if n.attrs.get('data-testid') == 'User-Name']
            body = '\n'.join(n.text().strip() for n in nodes if n.attrs.get('data-testid') == 'tweetText')
            media = [{'src': n.attrs.get('src', ''), 'alt': n.attrs.get('alt', '')} for n in nodes
                     if n.tag == 'img' and any(a.attrs.get('data-testid') == 'tweetPhoto' and under(n, a) for a in nodes)]
            if not body and not media:
                raise ProjectError('发现没有正文或图片的推文卡片，未提交不完整提取。')
            return {'post_id': post_id, 'author_account': identity[0] if identity else None,
                    'author_label': names[0] if names else None, 'published_at': date,
                    'permalink': identity[2] if identity else None, 'text': body, 'media': media,
                    'reply_to_id': container.attrs.get('data-in-reply-to-status-id'), 'relation': 'unknown'}

        item = record(card, quotes)
        item['quoted_posts'] = [record(q, []) for q in quotes]
        contexts = [n.text().strip() for n in descendants if n.attrs.get('data-testid') == 'socialContext']
        item['social_context'] = contexts
        if item['quoted_posts']:
            item['relation'] = 'quote'
        elif any(re.search(r'reposted|retweeted|转推|轉推|转帖|轉帖', c, re.I) for c in contexts):
            item['relation'] = 'repost'
        elif item['reply_to_id']:
            item['relation'] = 'reply'
        if not item['post_id']:
            warnings.append('有卡片缺少可靠帖子 ID 或永久链接，保留页面顺序，不猜测身份。')
        identity = item['post_id']
        if identity and identity in seen:
            if item == {k: v for k, v in seen[identity].items() if k != 'page_order'}:
                continue
            warnings.append(f'同一帖子 {identity} 有不同快照，均保留供复核。')
        if identity:
            seen[identity] = item
        item['page_order'] = len(posts) + 1
        posts.append(item)
    ids = {p['post_id'] for p in posts if p['post_id']}
    for p in posts:
        if p['reply_to_id'] and p['reply_to_id'] not in ids:
            warnings.append(f"帖子 {p['post_id']} 的上文 {p['reply_to_id']} 未保存在本文件。")
    return {'posts': posts, 'warnings': list(dict.fromkeys(warnings)), 'completeness': 'saved_dom_only'}


def extract_file(source: Path, *, force: bool = False) -> dict:
    source = source.resolve()
    from extract_article import decode_text_bytes
    raw = source.read_bytes()
    text, encoding = decode_text_bytes(raw)
    payload = extract_posts(text)
    out = source.parent / (source.stem + '_处理结果')
    meta_path = out / '00_资料信息.json'
    body_path = out / '01_原始正文.md'
    source_hash = digest(source)
    if out.exists() and not force:
        if meta_path.exists() and body_path.exists():
            old = json.loads(meta_path.read_text(encoding='utf-8-sig'))
            if old.get('original_sha256') == source_hash and old.get('output_sha256') == digest(body_path):
                for p in old.get('posts', []):
                    for owner in [p, *p.get('quoted_posts', [])]:
                        for image in owner.get('media', []):
                            if image.get('path'):
                                asset = out / image['path']
                                if not asset.is_file() or digest(asset) != image.get('sha256'):
                                    raise ProjectError('已有推文图片缺失或已修改，保留原稿并先核对。')
                return {'status': 'reused', 'source': str(source), 'result_directory': str(out), 'title': source.stem}
        raise ProjectError('推文提取目录已有不同或不完整成果，保留原稿；核对后才可使用 --force。')
    from image_assets import save_candidates
    for post in payload['posts']:
        for item in [post, *post['quoted_posts']]:
            item['media'] = save_candidates(source, item['media'], out)
    lines = [f'# {source.stem}', '', '> 推特存档按卡片保留，引用内容属于被引用账号；转推不等于完全认同。', '']
    for warning in payload['warnings']:
        lines.append(f'> {warning}')
    for p in payload['posts']:
        lines += ['', f"## 推文 {p['page_order']}　{p['post_id'] or 'ID 未知'}", '',
                  f"账号：{p['author_account'] or p['author_label'] or '未知'}；时间：{p['published_at'] or '未知'}；关系：{p['relation']}",
                  f"永久链接：{p['permalink'] or '未取得'}", '', p['text']]
        if p['reply_to_id']:
            lines += ['', f"回复对象 ID：{p['reply_to_id']}"]
        for q in p['quoted_posts']:
            lines += ['', f"### 引用内容　{q['author_account'] or q['author_label'] or '账号未知'}", '',
                      f"永久链接：{q['permalink'] or '未取得'}", '', q['text']]
        for origin in [p, *p['quoted_posts']]:
            for media in origin['media']:
                if media.get('path'):
                    lines += ['', f"![推文图片](<{media['path']}>)", f"图片替代文字：{media['alt']}；来源：{media['source']}（图中文字尚未 OCR 核验）"]
                else:
                    lines += ['', f"图片来源记录：`{media['source']}`；状态：{media['status']}；替代文字：{media['alt']}"]
    if digest(source) != source_hash:
        raise ProjectError('提取期间来源变化。')
    atomic_bytes(body_path, ('\n'.join(lines) + '\n').encode('utf-8'), overwrite=force)
    metadata = {'schema_version': 'x-html-1', 'material_type': 'x_html', 'authorship': 'mixed',
                'source_filename': source.name, 'original_sha256': source_hash, 'detected_encoding': encoding,
                'output_sha256': digest(body_path), **payload}
    write_json(meta_path, metadata, overwrite=force)
    return {'status': 'created', 'source': str(source), 'result_directory': str(out), 'title': source.stem,
            'post_count': len(payload['posts']), 'warning_count': len(payload['warnings'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(extract_file(args.source, force=args.force), ensure_ascii=False))
    except (ProjectError, OSError, ValueError) as error:
        parser.exit(2, f'错误：{error}\n')


if __name__ == '__main__':
    main()
