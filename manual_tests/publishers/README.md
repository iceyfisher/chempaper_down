# 校园网手动下载验证 / Manual campus-network checks

在已安装本项目的 `chem-paper-agent` 环境中，从项目根目录执行：

```powershell
python test.py --publisher rsc
python test.py --publisher acs
python test.py --publisher taylor
python test.py --doi 10.1021/acsomega.5c13377
python test.py --all
```

`test.py` 是唯一新增的联网下载测试入口，由用户在校园网运行。默认用例为本批次失败的 6 篇 RSC、1 篇 ACS Omega 和 17 篇 Taylor & Francis。每篇最多 360 秒，串行执行。下载默认以**有头**方式运行，会弹出 Edge 窗口——ACS/RSC/Taylor 的 Cloudflare 托管挑战会拦截无头会话，只有可见窗口才能通过验证。

已恢复原有的“导航前注册 Pydoll 验证处理器 → 挑战页加载时点击 Turnstile → 等待文章页面”流程。测试默认给验证处理和验证后页面等待各 60 秒；每篇导航总预算为整篇预算的 75%，两次尝试共享剩余时间。可以进一步延长：

```powershell
python test.py --doi 10.1039/c6ra08946a --article-timeout 480 --cloudflare-timeout 90
```

报告会记录实际预算。测试参数显式覆盖同名环境设置；主程序仍读取 `PAPER_TOOL_CLOUDFLARE_TIMEOUT`，已有 `.env` 值会覆盖新的默认值 60。主程序重启后生效。

每次运行创建独立 `_runs/<运行标识>/downloads`，逐篇更新 `report.json`。完整文件元数据和日志路径保存在每个条目的 `result` 中。将报告和失败条目的日志发回以继续分析。报告的通过表示正文和扫描发现的附件通过校验，不代表人工核对过官网附件总数。

Taylor & Francis 并非每篇文章都有 SI。完整扫描无附件时允许 `0/0`；文章或 Supplemental 页面受阻、Figshare 列表不可用时，扫描不完整，不能判成没有 SI。

已知偶发失败：个别 RSC 文章（如 `10.1039/c4dt01489h`）会在 pydoll 点击 Turnstile 时报 `Unable to resolve frameId for the iframe element`，同一篇在另一次运行中可正常通过。连续下载较多文章后失败率会上升，失败条目隔几分钟单独重跑通常能过。日志里出现 `Command timeout: ... timeout=60s` 表示页面已卡死，该篇按失败处理（不会挂到硬超时）。

Run the commands above from the repository root in the installed Python environment, on a network with publisher access. Each invocation creates an isolated run directory and saves a report after each DOI. Downloads open a visible Edge window by default: the Cloudflare managed challenge on ACS/RSC/Taylor blocks headless sessions. A persistent access challenge is reported as a failure, not as missing SI. Taylor & Francis articles may legitimately have no supplements; unresolved supplemental sources leave discovery incomplete.

Offline checks are separate: `python -m pytest tests/test_publisher_offline.py`. They use mocked browser/HTTP responses and do not import or execute `test.py`.
