# 最终 Markdown 模板

Skill 的 templates/ 是模板来源。material_type 表示载体，authorship 表示归属。文件头简要记录来源、作者／发布者、发表与保存时间、处理时间、核验日期；未知不猜。媒体另记时长、识别与回听范围。

现成文字使用 article-record-template.md 或 x-record-template.md，包含正文整理、来源、主题分析、必要背景、说法核验和参考目录。无需默认校订表、识别过程说明、逐段审校记录或重复全文。只有真实提取／OCR问题才简要说明。正文可直接整理成稿，保留有效信息和原文归属。

需要分开组装长文时，可把干净的提取正文或 02_整理正文.md 传入 assemble_record.py，metadata 的 content_mode 为 text_organization（文章／文字载体默认采用）。程序显示“正文整理”，不要求校正章节；生成的机械报告用于组装检查，不代表逐字审校，也不必链接进成稿。

媒体使用 video-record-template.md 或 audio-record-template.md，content_mode 为 media_transcript，仍要求校正全文和重要校正说明：
```text
<Python> scripts/assemble_record.py <02> <最终.md> --analysis <分析.md> --metadata <资料头.json> --report <覆盖报告.json>
<Python> scripts/audit_source_fidelity.py audit <02> <最终.md> <覆盖报告.json> --json
```

媒体覆盖报告放 00_阶段交接；普通文字无需强制生成。来源与整理者补充始终分层，概览、人物及图片索引按内容需要加入。图片路径与参考链接以源项目为基础；归档时重定位到成稿根目录，附件不复制。
