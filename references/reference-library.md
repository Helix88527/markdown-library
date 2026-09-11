# 参考资料查找与保存

参考目录可选；有则先检索，没有则按实际需要创建。用户指定库外参考目录只读使用，采用的原件可复制入项目，不移动来源。

先提出具体待校正或待核主张，查已有资料与往期成稿；证据缺口、时效或冲突触发公开搜索。优先原始发布、官方机构、法院、档案和原始研究。实际打开页面，不把搜索片段当成已读证据。多个转载同一报道不是多个独立来源。

```text
<Python> scripts/reference_library.py register <项目> --source <文件> --title <标题> --purpose <具体用途> --url <原始链接> --publisher <发布者> --published-at <日期>
<Python> scripts/reference_library.py download <项目> --url <HTTPS链接> --title <标题> --purpose <用途>
<Python> scripts/reference_library.py register <项目> --title <标题> --purpose <用途> --url <链接> --note <访问情况说明>
<Python> scripts/reference_library.py index <项目>
```

保存到参考资料/用户提供或联网补充，生成登记 JSON 和参考资料目录.md。按来源 URL 和哈希去重，HTML 同名伴随资源一并保存。生成目录由登记表重建，用户笔记单独保存。

下载支持选定的 HTTPS 文档，默认最大 50 MiB、超时 30 秒；遇到登录、权限或动态页面可正常浏览器保存可访问内容，或登记实际摘录／访问记录，不绕过访问限制。登录页、摘要、工具摘录和失败不标为全文。下载后默认待复核，实际检查内容后更新登记表状态并重建目录。

每项保留编号、标题、发布者、发表与访问日期、URL、本地文件、哈希、用途和保存状态。正文和核验表用相同 R 编号。终稿链接到参考目录，成稿仅保存最终 Markdown，重定位链接指向源项目的采用资料及图片；探索但未采用的材料可只留项目。
