# Skill 结构与维护

SKILL.md 是简短路由入口；references/workflow-overview.md 是供用户研究修改的流程图；各分支参考说明专有规则。templates/ 是统一模板来源，scripts/ 是可重复机械工作，tests/ 验证实际行为，README.md 提供安装与使用入口。

规则不再反复散落复制：媒体时长以 processing-modes.md 为准，参考资料以 reference-library.md 为准，模板以 markdown-template.md 为准，归档以 output-location-and-ingestion.md 为准，GPU 以 local-asr-and-efficiency.md 为准。

媒体状态 schema 与发布版本独立维护，支持逐项覆盖审计和成稿检索；使用推文元数据 x-html-1、参考登记 references-1、全文报告 fulltext-1 ，以及兼容旧目录的 bundle-1 和默认平铺的 flat-1。新字段与旧报告兼容边界写明，人工成果不自动迁移覆盖。

行为变更同步代码、测试、相关说明、模板、版本记录，进行实际功能验证和渲染检查。不要用只匹配文档措辞的测试代替行为检查；不把临时失败积累成无关永久关卡。独立验证使用合成输入与隔离输出，不污染成稿库或真实资料。
