# 校园网手动下载验证 / Manual campus-network checks

在已安装本项目的 `chem-paper-agent` 环境中，从项目根目录执行：

```powershell
python test.py --publisher rsc
python test.py --publisher acs
python test.py --publisher taylor
python test.py --doi 10.1021/acsomega.5c13377
python test.py --all
```

`test.py` 是唯一新增的联网下载测试入口，由用户在校园网运行。默认用例为本批次失败的 6 篇 RSC、1 篇 ACS Omega 和 17 篇 Taylor & Francis。每篇最多 360 秒，串行执行，全程后台浏览器。程序不会自动弹出验证窗口。

已恢复原有的“刷新页面 → Pydoll 验证处理 → 等待文章页面”流程。测试默认给验证处理和验证后页面等待各 60 秒；每篇导航总预算为整篇预算的 75%，两次尝试共享剩余时间。可以进一步延长：

```powershell
python test.py --doi 10.1039/c6ra08946a --article-timeout 480 --cloudflare-timeout 90
```

报告会记录实际预算。测试参数显式覆盖同名环境设置；主程序仍读取 `PAPER_TOOL_CLOUDFLARE_TIMEOUT`，已有 `.env` 值会覆盖新的默认值 60。主程序重启后生效。

每次运行创建独立 `_runs/<运行标识>/downloads`，逐篇更新 `report.json`。完整文件元数据和日志路径保存在每个条目的 `result` 中。将报告和失败条目的日志发回以继续分析。报告的通过表示正文和扫描发现的附件通过校验，不代表人工核对过官网附件总数。

Taylor & Francis 并非每篇文章都有 SI。完整扫描无附件时允许 `0/0`；文章或 Supplemental 页面受阻、Figshare 列表不可用时，扫描不完整，不能判成没有 SI。

Run the commands above from the repository root in the installed Python environment, on a network with publisher access. Each invocation creates an isolated run directory and saves a report after each DOI. No visible download browser is opened. A persistent access challenge is reported as a failure, not as missing SI. Taylor & Francis articles may legitimately have no supplements; unresolved supplemental sources leave discovery incomplete.

Offline checks are separate: `python -m pytest tests/test_publisher_offline.py`. They use mocked browser/HTTP responses and do not import or execute `test.py`.
