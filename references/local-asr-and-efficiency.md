# 本地 ASR GPU 和模型用量

本地 ASR 做音频转文字，本地脚本做提取、合并和哈希，强模型做专名校正、归属、理解、分析和核验。不能只读摘要就声称全文已校正。

本地识别器内部 token 不等于云端计费用量。GPU 主要缩短本地计算时间；避免重复读取、重写长稿及频繁工具往返才直接减少文字模型消耗。

## 设备预检

```text
<Python> scripts/asr_runtime.py probe --database-root <资料库>
<Python> scripts/asr_runtime.py benchmark --database-root <资料库> --media <媒体> --output <报告.json> --seconds 30 --start 300
```

使用 config.json 的 runtime_python。probe 检查设备、包版本和精度，只证明设备可探测；benchmark 实际解码 5 至 60 秒，完整消费惰性迭代器，记录加载时间、转写耗时和文本对照，聊天只返回短统计。

按当前任务已有授权在专用环境补齐缺失依赖。先查已有包和 DLL，仅安装缺项，保存通过验证的版本清单；不强制升级全套或系统驱动。CUDA 12 路线需要兼容的 CTranslate2、cuBLAS、cuDNN 9.x 和 CUDA 运行库。Windows 从专用环境 nvidia 包 bin 加载，也支持私有 cuda_library_dirs。不能仅凭检测到 GPU 宣称模型运行正常。

## 执行策略

- auto 未检测到 CUDA 默认停止，先修复；用户明确选 CPU 才用 --device cpu。初始显式设备选择传递到后续段。
- GPU 默认 float16，CPU 默认 int8。asr_batch_size 默认 1；比较 1、4 等批量后检查人名、数字、漏句和时间轴。长度接近也不代表语义通过，只有实际质量通过后才设置 asr_batch_validated=true 并提高批量；未验证的批量配置会被拒绝。
- continuous 使用 run-continuous，一个进程复用模型、逐段保存；staged 尊重用户停点。完成后释放模型，不设置常驻服务。
- 失败仅重跑未完成段；进度约每 60 秒一条，不把全文或大型 JSON 输出到聊天。
- 强模型分块校正后落盘；最终稿通过程序纳入 02，不重写第二份完整正文。

文件静置占磁盘；运行识别时才消耗内存、显存、CPU／GPU 与散热。桌面同步和索引是否影响性能须按实际设置判断；Skill 不自动迁移资料库或改变系统设置。

官方依据：
- https://github.com/SYSTRAN/faster-whisper
- https://docs.nvidia.com/deeplearning/cudnn/installation/latest/windows.html
