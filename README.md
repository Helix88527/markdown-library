# Markdown 资料库

将文章、网页、音视频和逐字稿整理为可追溯、可检索、便于持续积累的 Markdown 资料库。适用于课程笔记、访谈归档、读书资料、专题研究和公开资料整理。

- 按材料类型分流：现成文字整理正文，音视频本地转写并校正。
- 保留来源、时间戳、论证、例子与归属，区分原文、评论和整理者补充。
- 按任务需要补充主题分析、背景和说法核验。
- 最终 Markdown 平铺归档，图片和参考资料留在原项目，通过链接追溯。
- 支持阶段续跑、内容完整性校验、链接检查和同名冲突保护。

## 安装与调用

将本目录作为 `markdown-library` 放入支持 SKILL.md 的客户端技能目录。Codex 用户可放入 `~/.codex/skills/markdown-library`。

示例请求：

> 使用 $markdown-library，把我指定的课程文章整理入 Markdown 资料库，保留完整论证，补充必要背景和参考来源。

> 使用 $markdown-library，把这份访谈录音完整转写并整理入库，保留时间戳，只处理声音。

> 使用 $markdown-library，整理这些已保存的网页，区分作者正文和转载评论，并检查附件链接。

入口：[SKILL.md](SKILL.md)。详细流程：[流程图](references/workflow-overview.md)。

## 资料库设置

资料库可以使用任意目录名。在根目录建立 `.markdown-library-database.json`，内容为 `{}`，用于脚本识别。推荐布局：

```text
资料库根目录/
  .markdown-library-database.json
  原始资料/
    示例项目/
      来源文件
      参考资料/
  Markdown/
    示例项目.md
  工具/
    资料整理工具/
      config.json
      runtime/
      dictionaries/
```

将 [config.example.json](config.example.json) 复制到 `工具/资料整理工具/config.json`，按本机环境修改。`cooked_dir` 为最终 Markdown 目录；模型、解释器和词典使用相对于资料库根目录的路径。示例中的环境与模型路径需要自行配置，不代表已安装。macOS/Linux 的虚拟环境解释器通常位于 `bin/python`，Windows 通常位于 `Scripts/python.exe`。

用 `--database-root` 或 `locate_database.py --hint` 指定资料库；也可设置 `MARKDOWN_LIBRARY_ROOT`。未指定入库的整理任务可先在源项目生成 Markdown。用户已要求入库时直接沿用该授权，脚本不会因发现资料库就假定获得写入授权。

媒体状态管理器要求输入与状态位于同一资料库；库外材料可在原项目按流程整理，不应为了适配脚本自动移动原件。完整建库任务使用 `complete` 时会校验实际归档副本后清理临时文件；只做转写时保留输出，不调用该完成门。

## 依赖与验证

基础 HTML 提取、逐字稿导入和归档使用 Python 标准库。PDF、Word 与视觉功能按需安装 `requirements.txt`；本地语音识别另需 faster-whisper、CTranslate2、PyAV 与模型。参见 [本地 ASR](references/local-asr-and-efficiency.md)。

```sh
python -m unittest discover -s tests -q
python scripts/plan_output.py <来源路径> --database-root <资料库根目录> --ingest-confirmed --json
python scripts/export_github_package.py <新的导出目录>
```

参数 `--ingest-confirmed` / `--confirmed-by-user` 表示当前任务已要求相应操作，不是技能自身授予的权限。文件入库不等于公开发布材料。

本包不含资料库内容、个人配置、模型、虚拟环境、运行记录、历史仓库或图标。发布步骤见 [GITHUB_PUBLISHING.md](GITHUB_PUBLISHING.md)。现有 [LICENSE](LICENSE) 保留全部权利；这是可公开展示的源码包，并未授予开放源代码许可。
