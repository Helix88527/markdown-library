---
name: markdown-library
description: 将文章、网页、音视频和逐字稿整理为可追溯的 Markdown 资料库，包含正文整理、本地转写校正、按需分析核验及附件链接归档。用于建立或维护资料库、系统整理来源材料；仅需简短摘要时不启用完整流程。
---

# Markdown 资料库

技术调用名为 `$markdown-library`，版本以 [VERSION](VERSION) 为准。区分**媒体转写校正**与**现成文字整理**，把主要精力用在理解内容、核实说法和补充背景。保留材料的有效内容和归属，用户范围、停止点及已有选择优先。见 [流程说明](references/workflow-overview.md)。

适用于课程、访谈、读书资料、专题研究和网页收藏。分析与核验深度服从用户任务，不强制对所有材料做研究报告。资料库配置见 [README](README.md)。

## 1 只读检查

用 locate_database.py 定位资料库与私有配置，plan_output.py 规划位置，inspect_project.py 区分主材料、参考资料和已有成果。默认在原项目处理；用户要求入库时，成稿平铺到配置的 Markdown 目录；不为适配脚本移动原件。见 [位置和归档](references/output-location-and-ingestion.md)。

媒体先运行 media_stage_manager.py inspect；按有效状态续跑，不覆盖损坏状态、不跨越用户已选暂停点。参考目录不参与主材料批处理。有多条合理主路线且用户未选时，一次说明差异；已有选择不再询问。见 [材料选择](references/input-routing-and-transcript-import.md)。

## 2 按材料分流

| 材料 | 工作重点 |
|---|---|
| 视频、音频需完整转写 | 本地 faster-whisper、时间戳、技术切块、校正识别错误；[媒体模式](references/processing-modes.md) |
| TXT／MD／SRT／VTT 逐字稿 | 保留来源与已有时间戳；只要求文字整理时按现成文字处理，需要听校时按选择回听；[导入规则](references/input-routing-and-transcript-import.md) |
| 现成文章 HTML／MHTML／TXT／MD／PDF／DOCX | 提取正文与归属、整理结构、核实说法、补充背景；不默认逐字校对；[文章流程](references/article-workflow.md) |
| 推特／X HTML | 按卡片分离本人、引用、转推和回复，整理内容并核验；[推特流程](references/x-html-workflow.md) |

完整转写／完整听校不超过 90 分钟默认 continuous；超过时展示分段方案，用户未选模式才询问。连续授权直接沿用。技术切块与用户停顿分开，现成文字连续处理，不套用媒体阶段和时长。

每个新视频独立确定画面范围：不提取、信息密集画面、较全面。已有选择沿用；未决定前不扫描、截图或 OCR。启用后本地筛帧、去重、OCR、看图复核并保留依据。文章及推特原有图片可提取候选，替代文字不是 OCR。见 [画面流程](references/visual-materials-workflow.md)。

## 3 内容与证据

本地提取或导入保留原件和来源，检查正文边界、顺序、截断及页面噪声。正常文字没有识别问题时，不另造逐字校订任务、校正表或语义覆盖清单。发现乱码、OCR／提取错误或用户明确要求校对时，才做有依据的文字修正；作者的事实错误保留归因，放到核验层解释。

保留有效观点、论证、例子、数字、限定条件和不同意见。可调整标题、段落与列表，使 Markdown 便于阅读；不能把内容整理缩成丢失论证和例子的短摘要。转载原文、转载者评论与整理者补充分开。

参考资料按实际证据缺口查找。先查用户资料及当前成稿相关段落，需要时检索公开来源，保存实际采用资料并如实登记日期、URL、用途和保存状态。原件与参考资料留在源项目中。见 [参考资料](references/reference-library.md)。旧节目只能提供线索，不替代一手证据，不整库加载。

## 4 分析与核验

现成文字的重点是主题脉络、说法核验和必要背景；不预设原文措辞错误，也不把排版变化当作重要校正来报告。媒体转写仍逐段校正专名、断句和识别错误，核对 01→02，保留当前人工修订。

事实、观点、预测和整理者推断分开。核验结论使用有支持、部分支持、证据冲突、无法核实、不适用；不为价值判断硬判真假，不把相关写成因果。核验深度按主张的重要性和证据缺口安排，不凑固定条数。见 [写作规则](references/evidence-and-writing.md)。

## 5 成稿与归档

按 [模板说明](references/markdown-template.md) 写来源、正文内容、主题分析、背景、信息核验和参考目录。普通文字使用“正文整理”等标题；仅实际发现提取错误时附必要说明，不强制“重要校正与存疑项”。可直接写成稿，或用 assemble_record.py 纳入整理后的正文；不要求为机械排版额外生成 02。媒体继续原样纳入当前 02，并通过 [全文校验](references/source-fidelity-and-coverage.md)。

**默认仅将最终 Markdown 放到 Markdown/<标题>.md，不新建标题文件夹，不复制附件。**项目原稿和参考资料留在原处，成稿中的本地链接指向原始资料或原项目对应位置。用户已要求入库时，safe_copy_to_cooked.py --confirmed-by-user 按当前任务授权重定位链接、检查依赖并原子写入；只改链接所需路径，保留正文。同名不同内容不自动覆盖，比较后可用新文件名保留修订。旧目录归档仍可读，不批量迁移。

媒体 complete 校验源稿、重定位后的成稿稿和实际依赖。被最终稿引用的文件不得随临时清理删除；先将必要材料保存在项目永久位置。Word 只在用户要求或实际纳入成果时处理。见 [命名恢复](references/file-conventions.md)。

## 6 运行维护

ASR、OCR、格式处理和链接检查尽量本地运行，只返回短状态与异常。固定环境，GPU 探测后用短样本验证，auto 不静默退 CPU；continuous 复用模型，结束释放。缺失依赖按已有授权在专用环境补齐，不改变无关设置。见 [GPU 与用量](references/local-asr-and-efficiency.md)。

改动前检查 Git，保留已有修改；同步实现、测试、参考文档、模板、CHANGELOG，验证后报告结果；提交或推送按用户要求执行。见 [运行维护](references/program-runtime-and-maintenance.md)。
