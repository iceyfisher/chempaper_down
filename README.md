# ChemPaper Download

Download article PDFs and Supporting Information (SI) from a DOI list. The project provides a Web interface, a command-line tool, and a small HTTP API.

[中文说明](#中文说明) · [English](#english)

## 中文说明

### 支持范围

- ACS
- AIP Publishing
- AAAS / Science
- Royal Society of Chemistry
- Wiley
- Springer Nature / SpringerLink
- Elsevier / ScienceDirect，正文仅使用官方 Article Retrieval API；SI 仅使用 PII 推导的公开 `ars.els-cdn.com` PII/mmc

下载器会识别 PDF、ZIP、Office 文件、图片和视频等常见 SI 格式。

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

浏览器打开：http://127.0.0.1:8765

Web GUI 支持直接粘贴 DOI、上传 TXT/JSON/JSONL/CSV，或读取服务器能够访问的本地清单路径。并发数可选 1–4；常规 DOI 的硬超时可选 180、210 或 240 秒。大附件会在页面内下载失败后改走 Chromium。

### 命令行

直接提交一个或多个 DOI：

```powershell
paper-tool `
  --dois "10.1039/d6qo00853d,10.1021/acscatal.6c02592" `
  --concurrency 2 `
  --article-timeout 180 `
  --download-root .\downloads
```

从 TXT 读取：

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

### Elsevier

Elsevier 正文仅使用官方 Article Retrieval API。SI 从论文 PII 构造公开的 `ars.els-cdn.com/content/image/1-s2.0-<PII>-mmcN.<扩展名>` 地址下载。运行前必须在启动服务的同一个终端设置 key：

推荐将长期配置写入项目根目录的 `.env`（该文件已被 Git 忽略）：

```dotenv
ELSEVIER_API_KEY=your-key
PAPER_TOOL_ELSEVIER_TIMEOUT=600
```

服务和 DOI 子进程会自动读取启动工作目录中的 `.env`。已经存在的系统环境变量优先，不会被 `.env` 覆盖。也可以只为当前终端临时设置：

```powershell
$env:ELSEVIER_API_KEY = "your-key"
$env:PAPER_TOOL_ELSEVIER_TIMEOUT = "600"
```

API key 只通过服务进程环境传给 DOI 子进程，不会写入源码、结果 JSON 或 `_worker_runs/request.json`。程序先从 Article Retrieval API 的 JSON/XML 元数据提取 PII，缺失时再用 Article Metadata API 按 DOI 查询；浏览器只作为最后的 PII 元数据回退，不用于下载正文或 SI。正文 PDF 请求使用 `httpAccept=application/pdf`，不强制可能因权限产生 400 的 `view=FULL`。SI 的有限 PII/mmc 探测正常完成且得到 0 个候选时，表示已确认没有 SI，`0/0` 可以成为完整 bundle；缺少 PII、401/403/429 或网络错误则属于扫描未完成，不能解释成“文章没有 SI”。

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
常见状态：

- `success`：正文和发现的 SI 都下载成功
- `partial`：正文或部分 SI 成功，仍有附件失败
- `failed`：该 DOI 没有得到有效结果
- `timeout`：超过整篇硬预算
- `skipped_duplicate`：正文和 SI 已经完整校验

### 常见问题

`paper-tool-server` 无法识别：确认 Conda 环境已经激活，或使用 `$env:CONDA_PREFIX\Scripts\paper-tool-server.exe`。

任务显示 `partial`：先看 `downloads/_manifests/<doi>.json` 中失败附件的 `error`，再查看 diagnostics 里的日志路径。直接重新提交同一 DOI 即可，已校验文件不会重复下载。

Edge 卡住或网页加载很慢：先把并发降到 1，再把常规超时调到 210 或 240 秒。Wiley 已使用 600 秒整篇预算，单纯提高 GUI 超时不会改变它的预算。

## English Version

### Supported Publishers

* ACS
* AIP Publishing
* AAAS / Science
* Royal Society of Chemistry
* Wiley
* Springer Nature / SpringerLink
* Elsevier / ScienceDirect: the main article is retrieved exclusively through the official Article Retrieval API; Supporting Information (SI) is downloaded only from publicly accessible `ars.els-cdn.com` PII/mmc URLs derived from the article PII.

The downloader supports common SI formats, including PDF, ZIP, Microsoft Office files, images, and videos.

The same DOI can be submitted multiple times. If both the main article and SI have already passed validation, the task will be skipped automatically. If only one attachment is missing, the program preserves the existing article and previously downloaded SI files and downloads only the missing files.

### Requirements

* Windows 10 or Windows 11
* Python 3.11 or later
* Microsoft Edge
* A network environment with access to publisher websites
* Elsevier API key (required only for the official Elsevier API)

### Installation

It is recommended to create a dedicated Conda environment for the project:

```powershell
git clone https://github.com/iceyfisher/chempaper_down.git
cd chempaper_down

conda create -n chem-paper-agent python=3.11 -y
conda activate chem-paper-agent

python -m pip install --upgrade pip
python -m pip install -e .
```

If you prefer to install the dependencies from `requirements.txt` first:

```powershell
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

Check whether the command-line entry points are available:

```powershell
paper-tool --help
paper-tool-server --help
```

If PowerShell cannot find the entry points, you do not need to modify the system `PATH`. Run them directly with:

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

Open the following address in your browser:

`http://127.0.0.1:8765`

The Web GUI supports directly pasting DOIs, uploading TXT/JSON/JSONL/CSV files, or reading a local manifest path accessible to the server.

The concurrency level can be set from 1 to 4. The hard timeout for standard DOI tasks can be set to 180, 210, or 240 seconds. If a large attachment fails to download directly within the page, the downloader will fall back to Chromium.

### Command Line

Submit one or more DOIs directly:

```powershell
paper-tool `
  --dois "10.1039/d6qo00853d,10.1021/acscatal.6c02592" `
  --concurrency 2 `
  --article-timeout 180 `
  --download-root .\downloads
```

Read DOIs from a TXT file:

```powershell
paper-tool `
  --input .\example_doi_list.txt `
  --concurrency 2 `
  --article-timeout 180 `
  --download-root .\downloads `
  --json-output .\download_results.json
```

The default DOI field name in a JSON manifest is `doi`:

```powershell
paper-tool `
  --input .\example_agent_manifest.json `
  --doi-field doi `
  --download-root .\downloads
```

### Elsevier

For Elsevier, the main article is retrieved exclusively through the official Article Retrieval API. SI files are downloaded using publicly accessible URLs constructed from the article PII in the following format:

```text
ars.els-cdn.com/content/image/1-s2.0-<PII>-mmcN.<extension>
```

Before running the program, the API key must be configured in the same terminal used to start the service.

For persistent configuration, it is recommended to add the following settings to a `.env` file in the project root. This file is already ignored by Git:

```dotenv
ELSEVIER_API_KEY=your-key
PAPER_TOOL_ELSEVIER_TIMEOUT=600
```

The service and DOI worker subprocesses automatically load the `.env` file from the working directory where the service was started. Existing system environment variables take precedence and will not be overwritten by values in `.env`.

Alternatively, you can set the variables temporarily for the current terminal session:

```powershell
$env:ELSEVIER_API_KEY = "your-key"
$env:PAPER_TOOL_ELSEVIER_TIMEOUT = "600"
```

The API key is passed to DOI worker subprocesses only through the service process environment. It is never written to the source code, result JSON files, or `_worker_runs/request.json`.

The program first attempts to extract the PII from JSON/XML metadata returned by the Article Retrieval API. If the PII is unavailable, it queries the Article Metadata API using the DOI. The browser is used only as a final fallback for obtaining PII metadata and is **not** used to download the main article or SI files.

Main article PDF requests use:

```text
httpAccept=application/pdf
```

The program does not force `view=FULL`, which may result in HTTP 400 errors depending on API permissions.

If the limited PII/mmc SI probing process completes normally and returns zero candidates, the article is considered to have no SI, and a `0/0` SI result can therefore constitute a complete bundle.

In contrast, missing PII, HTTP `401`/`403`/`429` responses, or network errors indicate that the SI scan was not completed. These cases must **not** be interpreted as confirmation that the article has no SI.

### Download Results

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

Common task statuses:

* `success`: The main article and all discovered SI files were downloaded successfully.
* `partial`: The main article or some SI files were downloaded successfully, but one or more attachments still failed.
* `failed`: No valid result was obtained for the DOI.
* `timeout`: The task exceeded the hard time budget for the entire article.
* `skipped_duplicate`: The main article and SI files have already been fully downloaded and validated.

### Troubleshooting

**`paper-tool-server` is not recognized**

Make sure the Conda environment is activated, or run the executable directly:

```powershell
& "$env:CONDA_PREFIX\Scripts\paper-tool-server.exe"
```

**A task shows `partial`**

First inspect the `error` field for the failed attachment in:

```text
downloads/_manifests/<doi>.json
```

Then check the log path listed under `diagnostics`.

You can simply resubmit the same DOI. Files that have already passed validation will not be downloaded again.

**Edge becomes unresponsive or publisher pages load very slowly**

First reduce the concurrency level to `1`, then increase the standard timeout to `210` or `240` seconds.

Wiley already uses a 600-second per-article time budget, so simply increasing the GUI timeout will not change Wiley's internal budget.
