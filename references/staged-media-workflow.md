# 媒体阶段命令与恢复

使用配置中的固定 Python。开始先 inspect 检查原始媒体身份、已有状态和实际时长。短媒体默认 continuous，长媒体或 staged 须有相应选择授权；既有连续要求可作为确认依据。

```text
<Python> scripts/asr_runtime.py probe --database-root <根目录>
<Python> scripts/media_stage_manager.py --database-root <根目录> inspect <媒体> --json
<Python> scripts/media_stage_manager.py --database-root <根目录> init <媒体> --mode continuous
<Python> scripts/media_stage_manager.py --database-root <根目录> init <长媒体> --mode continuous --confirmed-by-user
<Python> scripts/media_stage_manager.py --database-root <根目录> run-continuous <媒体>
```

run-continuous 只适用于已初始化 continuous，单进程逐段保存并复用模型；不会更改 staged。完整范围用全局时间，切点两侧各 10 秒重叠。显式设备和精度从初始计划传递到后续段；auto 不静默退 CPU。

staged 用 init --mode staged --confirmed-by-user 初始化，然后 run-chunk 一段。用户说继续后运行 approve-next --confirmed-by-user，再 run-chunk；不能在循环中提前批准。最后一段完成后仍须确认 stage2-start。continuous 第一阶段结束后直接 stage2-start。

第一阶段原子提交正式 MD／SRT／JSON／JSONL 及 manifest，核对后由阶段管理器收口已登记分段中间件；未登记文件阻止清理，保留阶段交接。失败从下一未完成段继续，不重做好稿。

第二阶段逐段校正 02，写分析稿和资料头，通过 assemble_record 生成最终稿与 00_阶段交接/内容覆盖清单.json。完成命令：
```text
<Python> scripts/safe_copy_to_cooked.py <最终.md> --database-root <根目录> --confirmed-by-user --json
<Python> scripts/media_stage_manager.py --database-root <根目录> complete <媒体> --corrected <02> --final <最终.md> --ingest-confirmed --cooked <上一步返回的归档入口>
```

complete 验证正式第一阶段、当前 02、全文报告、最终入口及附件哈希，再按白名单清理交接。任何失败保留已验证成果。Word 可选，有输出才传 --visual；单独 Word 入库需相应授权。

旧覆盖清单与旧平铺成稿入口仍可显式续跑，不追溯改变旧暂停点。来源变化、人工修改、损坏状态或不同版本不能静默重置；先比较和修复。
