# ChemPaper Download

Download article PDFs and Supporting Information (SI) from a DOI list. The project provides a Web interface, a command-line tool, and a small HTTP API.

[中文说明](#中文说明) · [English](#english)

## 中文说明

### 支持范围

- ACS：`10.1021/*`
- AIP Publishing：`10.1063/*`
- AAAS / Science：`10.1126/*`
- Royal Society of Chemistry：`10.1039/*`
- Wiley：`10.1002/*`
- Springer Nature / SpringerLink：`10.1007/*`
- Elsevier / ScienceDirect：`10.1016/*`，正文优先使用官方 API，SI 浏览器路径目前是降级方案

下载器会识别 PDF、ZIP、Office 文件、图片和视频等常见 SI 格式。HTML 错误页、JSON 错误响应和无法识别的 `.bin` 不会写入最终 SI 目录。

同一 DOI 可以重复提交。正文和 SI 都通过校验时会直接跳过；如果只缺一个附件，程序会保留正文和已经下载好的 SI，只补缺失部分。

### 运行要求

- Windows 10 或 Windows 11
- Python 3.11 或更高版本
- Microsoft Edge
- 可访问出版社网页的网络环境
- Elsevier API key（仅 Elsevier 官方 API 需要）

### 安装

推荐为项目创建单独的 Conda 环境：

```powershell
git clone https://github.com/iceyfisher/chempaper_down.git
cd chempaper_down

conda create -n chem-paper-agent python=3.11 -y
conda activate chem-paper-agent

python -m pip install --upgrade pip
python -m pip install -e .
```

如果你想先按 `requirements.txt` 安装依赖：

```powershell
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

检查入口是否可用：

```powershell
paper-tool --help
paper-tool-server --help
```

PowerShell 找不到入口时，不必修改系统 PATH，可以直接运行：

```powershell
& "$env:CONDA_PREFIX\Scripts\paper-tool-server.exe" --help
python -m paper_tool.cli --help
python -m paper_tool.api --help
```

### Web GUI

启动本地服务：

```powershell
paper-tool-server `
  --host 127.0.0.1 `
  --port 8765 `
  --download-root .\downloads
```

如果终端找不到 `paper-tool-server`：

```powershell
& "$env:CONDA_PREFIX\Scripts\paper-tool-server.exe" `
  --host 127.0.0.1 `
  --port 8765 `
  --download-root .\downloads
```

浏览器打开：

```text
http://127.0.0.1:8765
```

Web GUI 支持直接粘贴 DOI、上传 TXT/JSON/JSONL/CSV，或读取服务器能够访问的本地清单路径。并发数可选 1–4；常规 DOI 的硬超时可选 180、210 或 240 秒。Wiley 使用单独的 600 秒整篇预算，大附件会在页面内下载失败后改走 Chromium 原生下载。

### 命令行

直接提交一个或多个 DOI：

```powershell
paper-tool `
  --dois "10.1039/d6qo00853d,10.1021/acscatal.6c02592" `
  --concurrency 2 `
  --article-timeout 180 `
  --download-root .\downloads
```

从示例 TXT 读取：

```powershell
paper-tool `
  --input .\example_doi_list.txt `
  --concurrency 2 `
  --article-timeout 180 `
  --download-root .\downloads `
  --json-output .\download_results.json
```

JSON 清单中的 DOI 字段默认名为 `doi`：

```powershell
paper-tool `
  --input .\example_agent_manifest.json `
  --doi-field doi `
  --download-root .\downloads
```

### HTTP API

启动 Web 服务后，可以提交：

```http
POST /api/jobs
Content-Type: application/json
```

```json
{
  "doi_text": "10.1039/d6qo00853d\n10.1021/acscatal.6c02592",
  "max_concurrency": 2,
  "article_timeout_seconds": 180
}
```

查询或取消任务：

```text
GET  /api/jobs/<job_id>
GET  /api/jobs/<job_id>/results
POST /api/jobs/<job_id>/cancel
```

### Elsevier

Elsevier 正文使用官方 Article Retrieval API。运行前在当前终端设置 key：

```powershell
$env:ELSEVIER_API_KEY = "your-key"
```

401/403 表示当前 key 或账号没有相应权限；429 表示配额或请求频率受限。这些状态不能解释成“文章没有 SI”。

### 下载结果

```text
downloads/
├─ <Publisher - Journal>/
│  ├─ paper/
│  └─ si/
├─ _jobs/
├─ _logs/
├─ _manifests/
└─ _worker_runs/
```

`_manifests` 记录来源 URL、文件类型、大小和 SHA-256，也负责断点续传。想保留自动补下载能力，就不要删除它。

常见状态：

- `success`：正文和发现的 SI 都下载成功
- `partial`：正文或部分 SI 成功，仍有附件失败
- `failed`：该 DOI 没有得到有效结果
- `timeout`：超过整篇硬预算
- `skipped_duplicate`：正文和 SI 已经完整校验

### 出版社手动验证

`manual_tests/publishers/` 中的脚本会实际访问出版社网站，不会被普通 `pytest` 自动收集。运行结果写入独立的 `_runs/` 目录，不会覆盖正式 `downloads/`。

```powershell
python manual_tests\publishers\run_publishers.py --case aip_jcp_pdf_and_zip_si
python manual_tests\publishers\run_publishers.py --case aaas_science_advances_all_si
```

也可以使用 `--all-enabled` 依次运行两个用例。AIP 用例检查正文 PDF 和 ZIP 格式的 SI；AAAS 用例检查阅读器页中的正文 PDF，并下载文章页发现的全部 SI。详细说明见 [manual_tests/publishers/README.md](manual_tests/publishers/README.md)。

### 常见问题

`paper-tool-server` 无法识别：确认 Conda 环境已经激活，或使用 `$env:CONDA_PREFIX\Scripts\paper-tool-server.exe`。

任务显示 `partial`：先看 `downloads/_manifests/<doi>.json` 中失败附件的 `error`，再查看 diagnostics 里的日志路径。直接重新提交同一 DOI 即可，已校验文件不会重复下载。

Edge 卡住或网页加载很慢：先把并发降到 1，再把常规超时调到 210 或 240 秒。Wiley 已使用 600 秒整篇预算，单纯提高 GUI 超时不会改变它的预算。

## English

### Supported publishers

- ACS: `10.1021/*`
- AIP Publishing: `10.1063/*`
- AAAS / Science: `10.1126/*`
- Royal Society of Chemistry: `10.1039/*`
- Wiley: `10.1002/*`
- Springer Nature / SpringerLink: `10.1007/*`
- Elsevier / ScienceDirect: `10.1016/*`; the official API is preferred for article PDFs, while browser-based SI discovery remains a fallback

The downloader recognizes common SI formats such as PDF, ZIP, Office documents, images, and video. HTML error pages, JSON error responses, and unknown `.bin` payloads are rejected before they reach the final SI directory.

Submitting the same DOI again is safe. A fully validated article is skipped. If one attachment is missing, the existing article PDF and valid SI files are reused while the missing file is retried.

### Requirements

- Windows 10 or Windows 11
- Python 3.11 or newer
- Microsoft Edge
- Network access to publisher websites
- An Elsevier API key for the official Elsevier API

### Installation

Create a dedicated Conda environment:

```powershell
git clone https://github.com/iceyfisher/chempaper_down.git
cd chempaper_down

conda create -n chem-paper-agent python=3.11 -y
conda activate chem-paper-agent

python -m pip install --upgrade pip
python -m pip install -e .
```

To install from `requirements.txt` first:

```powershell
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

Check the command-line entry points:

```powershell
paper-tool --help
paper-tool-server --help
```

If PowerShell cannot find the commands, use the environment path or Python modules directly:

```powershell
& "$env:CONDA_PREFIX\Scripts\paper-tool-server.exe" --help
python -m paper_tool.cli --help
python -m paper_tool.api --help
```

### Web GUI

Start the local server:

```powershell
paper-tool-server `
  --host 127.0.0.1 `
  --port 8765 `
  --download-root .\downloads
```

If `paper-tool-server` is not on PATH:

```powershell
& "$env:CONDA_PREFIX\Scripts\paper-tool-server.exe" `
  --host 127.0.0.1 `
  --port 8765 `
  --download-root .\downloads
```

Open:

```text
http://127.0.0.1:8765
```

The Web GUI accepts pasted DOI text, TXT/JSON/JSONL/CSV uploads, and local manifest paths visible to the server. Concurrency can be set from 1 to 4. The normal per-DOI hard timeout is 180, 210, or 240 seconds. Wiley uses a separate 600-second article budget and falls back to Chromium's native download manager for large attachments.

### Command line

Submit one or more DOI values directly:

```powershell
paper-tool `
  --dois "10.1039/d6qo00853d,10.1021/acscatal.6c02592" `
  --concurrency 2 `
  --article-timeout 180 `
  --download-root .\downloads
```

Read from the example text file:

```powershell
paper-tool `
  --input .\example_doi_list.txt `
  --concurrency 2 `
  --article-timeout 180 `
  --download-root .\downloads `
  --json-output .\download_results.json
```

JSON manifests use `doi` as the default field name:

```powershell
paper-tool `
  --input .\example_agent_manifest.json `
  --doi-field doi `
  --download-root .\downloads
```

### HTTP API

With the Web server running, submit:

```http
POST /api/jobs
Content-Type: application/json
```

```json
{
  "doi_text": "10.1039/d6qo00853d\n10.1021/acscatal.6c02592",
  "max_concurrency": 2,
  "article_timeout_seconds": 180
}
```

Inspect or cancel the job:

```text
GET  /api/jobs/<job_id>
GET  /api/jobs/<job_id>/results
POST /api/jobs/<job_id>/cancel
```

### Elsevier

Elsevier article PDFs use the official Article Retrieval API. Set the key in the current shell before starting the server or CLI:

```powershell
$env:ELSEVIER_API_KEY = "your-key"
```

HTTP 401/403 means the key or account lacks the required entitlement. HTTP 429 means a quota or rate limit was reached. None of these responses means that the article has no SI.

### Output

```text
downloads/
├─ <Publisher - Journal>/
│  ├─ paper/
│  └─ si/
├─ _jobs/
├─ _logs/
├─ _manifests/
└─ _worker_runs/
```

`_manifests` stores source URLs, detected file types, sizes, and SHA-256 hashes. It also drives resume behavior, so keep it if you want reliable retries.

Common statuses:

- `success`: the article and all discovered SI files were downloaded
- `partial`: the article or some SI files succeeded, but at least one item failed
- `failed`: no valid result was produced for the DOI
- `timeout`: the DOI exceeded its hard time budget
- `skipped_duplicate`: the article and SI were already validated

### Manual publisher checks

Scripts under `manual_tests/publishers/` contact publisher websites and are not collected by normal `pytest`. Each run writes to its own `_runs/` directory and does not overwrite the main `downloads/` tree.

```powershell
python manual_tests\publishers\run_publishers.py --case aip_jcp_pdf_and_zip_si
python manual_tests\publishers\run_publishers.py --case aaas_science_advances_all_si
```

Use `--all-enabled` to run both cases in sequence. The AIP case checks the article PDF and ZIP SI. The AAAS case checks the article PDF exposed by the reader page and every SI link discovered on the article page. See [manual_tests/publishers/README.md](manual_tests/publishers/README.md) for details.

### Troubleshooting

`paper-tool-server` is not recognized: activate the Conda environment or call `$env:CONDA_PREFIX\Scripts\paper-tool-server.exe` directly.

The result is `partial`: inspect the failed attachment in `downloads/_manifests/<doi>.json`, then open the log path listed in diagnostics. Submit the same DOI again; validated files will be reused.

Edge stalls or pages load slowly: reduce concurrency to 1, then set the normal timeout to 210 or 240 seconds. Wiley already uses a 600-second article budget, so changing the GUI timeout does not change its limit.

## Notes

- Download access depends on publisher availability, institutional access, and API entitlement.
- Do not commit API keys, downloaded papers, SI files, or run logs. The included `.gitignore` excludes the usual local output paths.
- The dependency audit is documented in [DEPENDENCY_CLEANUP.md](DEPENDENCY_CLEANUP.md).
