# 程序运行与维护

发布版本从 VERSION 读取。Skill 放规则、脚本、模板和测试，资料库工具包保存私有 config.json、固定 Python、模型和术语词典；不重复安装大型模型。

## 主要命令

| 工具 | 职责 |
|---|---|
| locate_database.py、plan_output.py | 只读定位资料库与项目输出 |
| inspect_project.py | 主材料、参考目录、已有成果角色清点 |
| media_stage_manager.py | 媒体状态、导入、分段转写、恢复和完成 |
| asr_runtime.py | GPU 预检、短样本、进程内模型复用 |
| extract_article.py、extract_x_html.py | 文章与推文分流提取 |
| image_assets.py、visual_materials.py | 原网页图片候选与视频关键画面 |
| reference_library.py | 资料下载、登记、目录和留证 |
| assemble_record.py | 媒体校正全文或文字整理正文与分析稿组装 |
| audit_source_fidelity.py | 新全文报告与旧覆盖清单校验 |
| flat_archive.py、safe_copy_to_cooked.py、archive_bundle.py | Markdown 平铺、链接回原项目，兼容旧目录 |
| search_cooked_archive.py | 直接检索当前成稿入口 |
| export_github_package.py | 白名单公开包，不包含私有材料 |

具体参数见各流程参考与 --help。媒体完整执行见 staged-media-workflow.md，模板见 markdown-template.md。

## 依赖

轻量提取、推文、模板组装和归档只用标准库。PDF／Word／视觉功能使用 requirements.txt；完整 ASR 使用固定运行环境中的 faster-whisper、CTranslate2、PyAV、imageio-ffmpeg 与本地模型。GPU 依赖按 local-asr-and-efficiency.md 预检和补齐，版本保存在私有环境清单；不把绝对用户路径、模型或运行环境发布。

## 验证

```text
<Python> -m compileall -q scripts tests
<Python> -m unittest discover -s tests -q
<Python> <skill-creator>/scripts/quick_validate.py <Skill目录>
<Python> scripts/export_github_package.py <全新的导出目录> --json
```

验证来源角色、90 分钟边界、旧状态暂停、全局时间、CPU 防静默回退、推特引用和缺失上文、文章归属、全文哈希与篡改、图片/参考链接、完整归档冲突和当前成稿检索。脚本测试不证明真实 ASR 运行；另做极短样本设备与文本对照。

## 版本发布

先检查 git status，保留用户改动；更新实现、测试、模板、参考文档、CHANGELOG。VERSION 唯一维护发布版本，状态 schema 单独兼容。验证通过后审查 diff 和 diff --check，提交与发布按当前用户要求执行。

只导出白名单脚本、文档、模板和测试。真实稿件、config.json、环境、模型、缓存、状态、渲染中间件、私有诊断与未明确可发布的图片排除。导出目标必须不存在，检查 EXPORT_MANIFEST 和敏感项扫描后才准备压缩。
