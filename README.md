# ChemPaper Download

Download article PDFs and Supporting Information (SI) from a DOI list, plus title search on OpenAlex and CNKI (知网) search. The project provides a neo-brutalist Web interface, a native desktop window, a command-line tool, and a small HTTP API.

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
- CNKI 中国知网：检索中文文献、提取 DOI 与中文标题并下载正文 PDF（知网文章没有 SI）。无法识别的 DOI 会自动作为兜底路由到知网检索

下载器会识别 PDF、ZIP、Office 文件、图片和视频等常见 SI 格式。

同一 DOI 可以重复提交。正文和 SI 都通过校验时会直接跳过；如果只缺一个附件，程序会保留正文和已经下载好的 SI，只补缺失部分。

### 归档目录

每篇文章归档到独立目录，目录名由 DOI、年份和期刊组成，内含 `pdf/` 与 `si/` 两个子文件夹：

```text
downloads/
├─ 10.1021_acs.catal.6c02592_2026_ACS Catalysis/
│  ├─ pdf/          # 正文 <doi>.pdf
│  └─ si/           # 补充材料 <doi>_si_<hash>.<ext>
├─ 10.19799_j.cnki.2095-4239.2023.0001_2023_储能科学与技术/
│  └─ pdf/          # 知网文章只有正文
├─ _jobs/           # 任务状态
├─ _logs/           # DOI 子进程日志
├─ _manifests/      # 每篇论文的结果清单
├─ _search_runs/    # 知网检索子进程结果
└─ _worker_runs/    # 子进程工作目录
```

年份取自出版社页面的 citation 元数据或 Elsevier API 的 coverDate；确实拿不到时使用 `unknown`。旧版 `<Publisher - Journal>/paper/` 目录仍会被重复下载检查识别，已有的下载不会重复。

### 运行要求

- Windows 10 或 Windows 11
- Python 3.11 或更高版本
- Microsoft Edge
- 可访问出版社网页的网络环境（校园网权限）
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
paper-tool-app --help
```

### 桌面应用 / Web GUI

推荐直接启动桌面窗口（pywebview 原生窗口，关闭窗口即退出）：

```powershell
paper-tool-app
```

未安装 pywebview 时会自动改用默认浏览器打开同一个界面。也可以只用 Web 服务：

```powershell
paper-tool-server `
  --host 127.0.0.1 `
  --port 8765 `
  --download-root .\downloads
```

浏览器打开：http://127.0.0.1:8765

界面分四个标签页：

1. **① DOI 批量下载**：粘贴 DOI / DOI URL，或上传 TXT/JSON/JSONL/CSV 清单、读取本地清单路径。并发数 1–4，硬超时 180/210/240 秒。
2. **② 学术标题检索（OpenAlex）**：输入标题或关键词检索 OpenAlex 并提取 DOI；每条结果旁有三个按钮——**复制 DOI**、**下载原文**（跳过 SI）、**下载原文+SI**。下方支持高通量批量解析：一行一个标题（最多 500 条），逐条解析出最匹配的 DOI，可勾选后整批下载或一键复制全部 DOI。
3. **③ 知网检索（CNKI）**：通过独立 Edge 子进程访问知网页面检索中文文献，返回标题、年份、DOI（如有）。知网文章没有 SI，只提供复制 DOI 与下载原文。
4. **④ 任务与结果**：实时轮询任务进度，展示每篇论文的状态、出版社、期刊、年份、SI 计数与耗时。

"下载原文"（不带 SI）通过任务级/条目级 `download_si` 开关实现，各出版社 adapter 会完整跳过 SI 扫描，结果可以直接达到 `success`。

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

### 检索 API

```http
GET  /api/search/academic?q=<标题或关键词>&limit=10     # OpenAlex 相关性检索
POST /api/search/academic/batch {"titles": ["...", "..."]}  # 批量标题→DOI 解析（带相似度）
GET  /api/search/cnki?q=<中文标题或关键词>              # 知网检索（独立浏览器子进程）
POST /api/jobs/items {"items": [{"doi": "...", "download_si": true}]}  # 按条目提交下载
```

`/api/jobs/items` 的每个条目可以携带 `title_query`、`article_url`（知网直跳）与独立的 `download_si` 覆盖。所有既有端点（`/api/jobs`、`/api/jobs/upload`、`/api/jobs/path`、`/api/agent/*`）都新增了 `download_si` 字段，默认 `true`，行为与旧版一致。

OpenAlex 免费无需 key；在 `.env` 中设置 `OPENALEX_MAILTO=you@example.com` 可进入 polite pool 获得更稳定的限流。

### Elsevier

Elsevier 正文仅使用官方 Article Retrieval API。SI 从论文 PII 构造公开的 `ars.els-cdn.com/content/image/1-s2.0-<PII>-mmcN.<扩展名>` 地址下载。运行前必须在启动服务的同一个终端设置 key：

推荐将长期配置写入项目根目录的 `.env`（该文件已被 Git 忽略）：

```dotenv
ELSEVIER_API_KEY=your-key
PAPER_TOOL_ELSEVIER_TIMEOUT=600
OPENALEX_MAILTO=you@example.com
```

服务和 DOI 子进程会自动读取启动工作目录中的 `.env`。已经存在的系统环境变量优先，不会被 `.env` 覆盖。也可以只为当前终端临时设置：

```powershell
$env:ELSEVIER_API_KEY = "your-key"
$env:PAPER_TOOL_ELSEVIER_TIMEOUT = "600"
```

API key 只通过服务进程环境传给 DOI 子进程，不会写入源码、结果 JSON 或 `_worker_runs/request.json`。正文 PDF 请求使用 `httpAccept=application/pdf`。SI 的有限 PII/mmc 探测正常完成且得到 0 个候选时，表示已确认没有 SI，`0/0` 可以成为完整 bundle；缺少 PII、401/403/429 或网络错误则属于扫描未完成，不能解释成"文章没有 SI"。选择"下载原文（不带 SI）"时会完整跳过该探测。

### 下载结果

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

知网检索/下载失败：确认校园网可达 `kns.cnki.net` 且机构已授权；知网偶发验证码时需要人工在浏览器完成一次验证后重试。无法匹配 DOI 的中文文章可从知网检索结果直接点"下载原文"，程序会通过文章页直链完成下载。

## English Version

### Supported Publishers

* ACS
* AIP Publishing
* AAAS / Science
* Royal Society of Chemistry
* Wiley
* Springer Nature / SpringerLink
* Elsevier / ScienceDirect: the main article is retrieved exclusively through the official Article Retrieval API; Supporting Information (SI) is downloaded only from publicly accessible `ars.els-cdn.com` PII/mmc URLs derived from the article PII.
* CNKI (中国知网): search Chinese literature, extract DOI and Chinese title, and download the main PDF (CNKI articles have no SI). Unrecognized DOIs fall back to a CNKI search automatically.

The downloader supports common SI formats, including PDF, ZIP, Microsoft Office files, images, and videos.

The same DOI can be submitted multiple times. If both the main article and SI have already passed validation, the task will be skipped automatically. If only one attachment is missing, the program preserves the existing article and previously downloaded SI files and downloads only the missing files.

### Archive Layout

Each article is archived into its own folder named after DOI, year and journal, with `pdf/` and `si/` subfolders:

```text
downloads/
├─ 10.1021_acs.catal.6c02592_2026_ACS Catalysis/
│  ├─ pdf/
│  └─ si/
├─ _jobs/
├─ _logs/
├─ _manifests/
├─ _search_runs/
└─ _worker_runs/
```

The year comes from citation meta tags or the Elsevier coverDate; `unknown` is used when unavailable. The legacy `<Publisher - Journal>/paper/` layout is still recognized by the duplicate check, so existing downloads are never repeated.

### Requirements

* Windows 10 or Windows 11
* Python 3.11 or later
* Microsoft Edge
* A network environment with access to publisher websites (campus network entitlement)
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

Check whether the command-line entry points are available:

```powershell
paper-tool --help
paper-tool-server --help
paper-tool-app --help
```

### Desktop App / Web GUI

Launch the native desktop window (pywebview shell; closing the window quits):

```powershell
paper-tool-app
```

Without pywebview the same UI opens in the default browser. Web-only mode:

```powershell
paper-tool-server --host 127.0.0.1 --port 8765 --download-root .\downloads
```

Open `http://127.0.0.1:8765`. The UI has four tabs:

1. **DOI batch download** — paste DOIs or upload TXT/JSON/JSONL/CSV manifests; concurrency 1–4, hard timeout 180/210/240 s.
2. **OpenAlex title search** — search by title/keyword and extract DOIs; every result row offers **Copy DOI**, **Download PDF** (no SI) and **Download PDF+SI**. A high-throughput batch box resolves up to 500 titles to their best-matching DOIs.
3. **CNKI search** — an isolated Edge subprocess searches CNKI for Chinese literature and returns title/year/DOI; CNKI rows offer Copy DOI and Download PDF only (no SI).
4. **Jobs & results** — live polling of job progress with status, publisher, journal, year, SI counters and elapsed time.

"Download PDF" (without SI) is implemented through a job-/item-level `download_si` switch; adapters skip the SI scan entirely so such tasks can reach `success` directly.

### Search API

```http
GET  /api/search/academic?q=<query>&limit=10
POST /api/search/academic/batch {"titles": ["...", "..."]}
GET  /api/search/cnki?q=<query>
POST /api/jobs/items {"items": [{"doi": "...", "download_si": true}]}
```

Each `/api/jobs/items` entry may carry `title_query`, `article_url` (direct CNKI article link) and a per-item `download_si` override. All existing endpoints accept a `download_si` field (default `true`, matching the legacy behavior).

OpenAlex is free; set `OPENALEX_MAILTO=you@example.com` in `.env` to join the polite pool.

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

For persistent configuration, add the following settings to a `.env` file in the project root (already ignored by Git):

```dotenv
ELSEVIER_API_KEY=your-key
PAPER_TOOL_ELSEVIER_TIMEOUT=600
```

The service and DOI worker subprocesses automatically load the `.env` file from the working directory where the service was started. Existing system environment variables take precedence. Alternatively, set the variables temporarily for the current terminal session:

```powershell
$env:ELSEVIER_API_KEY = "your-key"
$env:PAPER_TOOL_ELSEVIER_TIMEOUT = "600"
```

The API key is passed to DOI worker subprocesses only through the service process environment. It is never written to the source code, result JSON files, or `_worker_runs/request.json`.

Main article PDF requests use `httpAccept=application/pdf`. If the limited PII/mmc SI probing process completes normally and returns zero candidates, the article is considered to have no SI. Missing PII, HTTP 401/403/429 responses, or network errors indicate that the SI scan was not completed and must not be interpreted as "no SI". When "Download PDF (no SI)" is requested the probing is skipped entirely.

### Download Results

```text
downloads/<doi>_<year>_<journal>/pdf|si
```

Common task statuses:

* `success`: The main article and all discovered SI files were downloaded successfully.
* `partial`: The main article or some SI files were downloaded successfully, but one or more attachments still failed.
* `failed`: No valid result was obtained for the DOI.
* `timeout`: The task exceeded the hard time budget for the entire article.
* `skipped_duplicate`: The main article and SI files have already been fully downloaded and validated.

### Troubleshooting

**`paper-tool-server` is not recognized** — make sure the Conda environment is activated, or run the executable directly from `$env:CONDA_PREFIX\Scripts`.

**A task shows `partial`** — inspect the `error` field in `downloads/_manifests/<doi>.json` and the log path under `diagnostics`. Resubmitting the same DOI is safe; validated files are not downloaded again.

**Edge becomes unresponsive or pages load slowly** — reduce concurrency to `1` and raise the timeout to `210` or `240` seconds. Wiley already uses a 600-second per-article budget.

**CNKI search/download fails** — verify `kns.cnki.net` is reachable through the campus network and the institution is entitled. When CNKI shows a CAPTCHA, solve it once manually in a browser and retry. Chinese articles without a DOI can still be downloaded directly from a CNKI search row via the article-page link.
