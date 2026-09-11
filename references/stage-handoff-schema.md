# 状态和兼容边界

发布版本由 VERSION 读取；media_stage_manager 的 SCHEMA_VERSION 独立决定持久状态兼容，当前使用 schema 1。已有任务的 staged 暂停点、来源身份、分段范围、人工修改与清理白名单继续有效，不自动迁移。

state.json 是媒体进度事实源，关键对象包括 source、audio、execution、environment、stage1、stage2、temporary_owned_files。各段保存状态、尝试次数、来源与输出哈希、全局时间范围、实际设备／精度和识别环境；新增 batch_size 与 transcription_seconds 为可选环境信息，旧记录缺失不视为损坏。

01 正式四格式通过 manifest 校验。当前 02 人工稿优先。fulltext-1 是新增的全文覆盖报告格式，记录来源哈希、最终哈希和机械检查结果；旧逐项覆盖报告仍受原检查规则约束。旧 bundle-1 归档清单继续读取。新 flat-1 只存成稿 Markdown，附件链接回源项目；阶段状态记录源稿与成稿分别的哈希及依赖。

complete 对新平铺稿比较重定位后的正文并检查源处附件；对旧目录稿仍验证归档包。被成稿引用的临时路径须先转存永久位置，不得在完成清理时删除。旧稿继续按旧报告和明确指定入口验证。临时路径必须属于当前任务、在白名单内、普通文件且哈希有效；不跟随 symlink/junction，不递归强删。用户说停或发现未知文件时保留交接，未完成任务不提前清理。

执行细节见 staged-media-workflow.md，全文格式见 source-fidelity-and-coverage.md，归档见 output-location-and-ingestion.md。
