# 视频画面提取和复核

每个新视频项目单独确定不提取、信息密集（推荐）或较全面。已有选择沿用；未确定前只读 inspect，不扫描场景、不截图、不 OCR。文字转写连续授权不代表启用画面。

启用后用 visual_materials.py 的场景变化、信息密度和感知哈希形成候选，不逐帧送入模型。候选过多时按脚本提示确定范围，避免无意义重复。保存可复核原帧，全局时间戳进入文件名。

```text
<Python> scripts/visual_materials.py extract <视频> --output-dir <视觉工作区> --reference <02> --reference <采用资料> --confirmed-by-user --json
```

默认本地 OCR；缺少引擎要补齐或记录待处理，不能用 skip-ocr 掩盖未完成。查阅相邻逐字稿、项目参考资料、词典与需要的公开来源，再复核截图。manifest 各项分别填写 ocr_raw、corrected_text、reference_findings、description、inference、related_transcript、related_theme、uncertainties 和 review_status。不能只把状态改成 complete。

图片有新增信息才入稿，文字、表格、图表都保留出处和时刻；OCR 原文、校正与整理者推断分开。图片通常用 <Markdown stem>_assets/ 相对路径，可在 02 对应位置加入后再组装，也可作为分析部分的独立图表说明。不要改动组装后的全文保护区。

embed-markdown 写入受管视觉章节并保护其他正文，同名不同哈希资产不覆盖。Word 视觉附件按用户需要生成，不因启用图片强制增加 Word。build-docx 仅在所有条目复核完成后运行，必须渲染并检查全部页面后交付。

成稿根目录仅保存最终 Markdown；实际引用的图片留在源项目永久 assets 中，归档时把链接重定位到原文件。候选工作区与最终 assets 分开，永久图片不属于临时白名单。用户文件出现时停止自动清理。
