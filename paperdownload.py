import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
from pathlib import Path
from urllib.parse import (
    parse_qs,
    unquote,
    urljoin,
    urlparse,
)

from pydoll.browser.chromium import Edge
from pydoll.browser.options import ChromiumOptions
from pydoll.browser.managers.temp_dir_manager import TempDirectoryManager


# ============================================================
# 0. Pydoll Windows cleanup patch
# ============================================================
#
# Windows + Edge 退出时：
#
# Chromium 有时会提前删除 *.wal / 临时 profile 文件，
# 随后 Pydoll shutil.rmtree() 再删一次就会产生：
#
# FileNotFoundError
#
# 这里只忽略 FileNotFoundError。
# ============================================================

_ORIGINAL_CLEANUP_HANDLER = (
    TempDirectoryManager.handle_cleanup_error
)


def _patched_cleanup_error_handler(
    self,
    func,
    path,
    exc_info,
):
    _, exc_value, _ = exc_info

    if isinstance(
        exc_value,
        FileNotFoundError,
    ):
        return

    return _ORIGINAL_CLEANUP_HANDLER(
        self,
        func,
        path,
        exc_info,
    )


if os.name == "nt":
    TempDirectoryManager.handle_cleanup_error = (
        _patched_cleanup_error_handler
    )


# ============================================================
# 1. 路径配置
# ============================================================

SCRIPT_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

DOI_LIST_PATH = (
    SCRIPT_DIR
    / "doi_list.txt"
)

DOWNLOAD_ROOT = (
    SCRIPT_DIR
    / "downloads"
)

MANIFEST_PATH = (
    DOWNLOAD_ROOT
    / "download_manifest.json"
)


# ============================================================
# Edge 原生下载暂存目录
# ============================================================
#
# RSC 正文：
#
#   real click
#       ↓
#   Edge Download Manager
#       ↓
#   _browser_downloads
#       ↓
#   paper/
#
#
# Wiley 正文：
#
#   Reader → PDF JS click
#       ↓
#   Edge Download Manager
#       ↓
#   _browser_downloads
#       ↓
#   paper/
#
#
# Blob：
#
#   ACS正文
#   ACS SI
#   RSC SI
#   Wiley SI
# ============================================================

BROWSER_DOWNLOAD_DIR = (
    DOWNLOAD_ROOT
    / "_browser_downloads"
)


DOWNLOAD_ROOT.mkdir(
    parents=True,
    exist_ok=True,
)

BROWSER_DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# ============================================================
# 2. Timeout
# ============================================================

ELEMENT_TIMEOUT = 30

# RSC 最多等待 3 min
RSC_RUNTIME_TIMEOUT = 180

# Wiley Article / Reader 最多等待 3 min
WILEY_RUNTIME_TIMEOUT = 180

# Wiley PDF 下载
WILEY_PDF_DOWNLOAD_TIMEOUT = 180

# 单个大附件最多 5 min
DOWNLOAD_TIMEOUT = 300

# ACS Cloudflare helper
CLOUDFLARE_TIMEOUT = 15

PAGE_SETTLE_TIME = 3


# ============================================================
# 3. Publisher
# ============================================================

PUBLISHER_ACS = "ACS"

PUBLISHER_RSC = "RSC"

PUBLISHER_WILEY = "WILEY"


PUBLISHER_FULL_NAMES = {

    PUBLISHER_ACS:
        "American Chemical Society",

    PUBLISHER_RSC:
        "Royal Society of Chemistry",

    PUBLISHER_WILEY:
        "Wiley",
}


# ============================================================
# 4. Journal fallback
# ============================================================

ACS_DOI_JOURNAL_CODES = {

    "acscatal":
        "ACS Catalysis",

    "orglett":
        "Organic Letters",

    "joc":
        "The Journal of Organic Chemistry",

    "jacs":
        "Journal of the American Chemical Society",
}


RSC_JOURNAL_CODES = {

    "qo":
        "Organic Chemistry Frontiers",

    "ra":
        "RSC Advances",

    "cc":
        "Chemical Communications",

    "dt":
        "Dalton Transactions",

    "cp":
        "Physical Chemistry Chemical Physics",

    "ta":
        "Journal of Materials Chemistry A",

    "tb":
        "Journal of Materials Chemistry B",

    "tc":
        "Journal of Materials Chemistry C",
}


WILEY_DOI_JOURNAL_CODES = {

    "anie":
        "Angewandte Chemie International Edition",
}


# ============================================================
# 5. DOI parser
# ============================================================

DOI_PATTERN = re.compile(
    r"""
    10\.
    \d{4,9}
    /
    [A-Za-z0-9._;()/:+-]+
    """,
    re.VERBOSE
    | re.IGNORECASE,
)


def normalize_doi(
    doi: str,
) -> str:

    doi = doi.strip()

    doi = re.sub(
        r"^https?://(?:dx\.)?doi\.org/",
        "",
        doi,
        flags=re.IGNORECASE,
    )

    # 去掉末尾常见标点
    doi = doi.rstrip(
        ".,;:]}>'\""
    )

    return doi.lower()


def extract_doi_from_line(
    line: str,
):

    match = DOI_PATTERN.search(
        line
    )

    if not match:
        return None

    return normalize_doi(
        match.group(0)
    )


def read_doi_list():
    """
    支持：

    https://doi.org/10.1002/anie.2761225

    以及：

    https://doi.org/10.1002/anie.2761225 （Wiley测试）
    """

    if not DOI_LIST_PATH.exists():

        raise FileNotFoundError(
            f"找不到 DOI 文件：{DOI_LIST_PATH}"
        )

    result = []

    seen = set()

    for line in DOI_LIST_PATH.read_text(
        encoding="utf-8-sig"
    ).splitlines():

        line = line.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        doi = extract_doi_from_line(
            line
        )

        if not doi:

            print(
                f"⚠️ 无法识别 DOI：{line}"
            )

            continue

        # doi_list.txt 自身重复
        if doi in seen:

            print(
                f"DOI列表内部重复 忽略：{doi}"
            )

            continue

        seen.add(
            doi
        )

        result.append(
            doi
        )

    return result


# ============================================================
# 6. 文件名
# ============================================================

def clean_path_component(
    name: str,
) -> str:

    name = unquote(
        name
    )

    name = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        name,
    )

    name = re.sub(
        r"\s+",
        " ",
        name,
    )

    return (
        name
        .strip()
        .strip(".")
    )


def doi_to_filename(
    doi: str,
) -> str:
    """
    10.1002/anie.2761225

    →

    10.1002_anie.2761225
    """

    return clean_path_component(
        doi.replace(
            "/",
            "_",
        )
    )


# ============================================================
# 7. SHA256
# ============================================================

def sha256_file(
    path: Path,
):

    digest = hashlib.sha256()

    with path.open(
        "rb"
    ) as file:

        while True:

            block = file.read(
                1024 * 1024
            )

            if not block:
                break

            digest.update(
                block
            )

    return digest.hexdigest()


# ============================================================
# 8. PDF 有效性检查
# ============================================================

def existing_pdf_is_valid(
    path: Path,
):
    """
    用于：
    - 下载完成后的验证
    - DOI 全局重复检查

    检查：

    %PDF
    %%EOF
    replacement-byte corruption
    """

    try:

        if not path.exists():
            return False

        if not path.is_file():
            return False

        size = (
            path.stat()
            .st_size
        )

        if size <= 0:
            return False

        with path.open(
            "rb"
        ) as file:

            head = file.read(
                64
            )

            file.seek(
                max(
                    0,
                    size - 4096,
                )
            )

            tail = file.read()

        if not head.startswith(
            b"%PDF"
        ):

            return False

        if b"%%EOF" not in tail:

            return False

        # 之前 ACS binary 错误 UTF-8 重编码特征
        if (
            b"\xef\xbf\xbd"
            in head
        ):

            return False

        return True

    except Exception:

        return False


# ============================================================
# 9. ★ DOI 全局去重
# ============================================================

def find_existing_paper(
    doi: str,
):
    """
    扫描：

        downloads/**/paper/

    查找：

        <DOI>.pdf

    示例：

        10.1002_anie.2761225.pdf


    如果发现有效正文 PDF：

        整个 DOI 跳过

    不访问网页
    不检查 SI
    不下载 SI
    """

    doi_filename = (
        doi_to_filename(
            doi
        )
    )

    if not DOWNLOAD_ROOT.exists():
        return None

    for paper_dir in DOWNLOAD_ROOT.rglob(
        "paper"
    ):

        if not paper_dir.is_dir():
            continue

        expected = (
            paper_dir
            /
            f"{doi_filename}.pdf"
        )

        if existing_pdf_is_valid(
            expected
        ):

            return expected

    return None


# ============================================================
# 10. 创建出版社 / 期刊目录
# ============================================================

def make_journal_directories(
    publisher: str,
    journal: str,
):

    publisher_full = (
        PUBLISHER_FULL_NAMES.get(
            publisher,
            publisher,
        )
    )

    folder_name = (
        clean_path_component(
            f"{publisher_full} - {journal}"
        )
    )

    journal_dir = (
        DOWNLOAD_ROOT
        / folder_name
    )

    paper_dir = (
        journal_dir
        / "paper"
    )

    si_dir = (
        journal_dir
        / "si"
    )

    paper_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    si_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    return (
        journal_dir,
        paper_dir,
        si_dir,
    )


# ============================================================
# 11. Publisher 分类
# ============================================================

def publisher_hint_from_doi(
    doi: str,
):

    if doi.startswith(
        "10.1021/"
    ):

        return PUBLISHER_ACS

    if doi.startswith(
        "10.1039/"
    ):

        return PUBLISHER_RSC

    if doi.startswith(
        "10.1002/"
    ):

        return PUBLISHER_WILEY

    return None


def classify_publisher(
    landing_url: str,
    doi: str,
):

    # DOI prefix 优先
    hint = (
        publisher_hint_from_doi(
            doi
        )
    )

    if hint:

        return hint

    host = (
        urlparse(
            landing_url
        )
        .netloc
        .lower()
    )

    if (
        "acs.org"
        in host
    ):

        return PUBLISHER_ACS

    if (
        "rsc.org"
        in host
    ):

        return PUBLISHER_RSC

    if (
        "wiley.com"
        in host
        or
        "wiley-vch.de"
        in host
    ):

        return PUBLISHER_WILEY

    return None


# ============================================================
# 12. Chromium staging
# ============================================================

def clear_browser_download_dir():

    BROWSER_DOWNLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    for path in list(
        BROWSER_DOWNLOAD_DIR.iterdir()
    ):

        try:

            if path.is_file():

                path.unlink()

            elif path.is_dir():

                shutil.rmtree(
                    path,
                    ignore_errors=True,
                )

        except Exception as exc:

            print(
                "⚠️ staging清理失败：",
                repr(exc),
            )


def staging_snapshot():

    return {

        path.resolve()

        for path
        in BROWSER_DOWNLOAD_DIR.iterdir()

        if path.is_file()
    }


# ============================================================
# 13. 监控 Edge 原生下载
# ============================================================

async def wait_for_native_download(
    before_files,
    timeout=DOWNLOAD_TIMEOUT,
):
    """
    Chromium 下载过程通常：

        filename.pdf.crdownload
                ↓
        filename.pdf

    最终文件大小连续稳定 3 次后认为完成。
    """

    loop = asyncio.get_running_loop()

    deadline = (
        loop.time()
        + timeout
    )

    last_path = None

    last_size = None

    stable_count = 0

    while (
        loop.time()
        <
        deadline
    ):

        current = (
            staging_snapshot()
        )

        new_files = (
            current
            -
            before_files
        )

        # ====================================================
        # 下载中
        # ====================================================

        partials = [

            path

            for path
            in new_files

            if path.name
            .lower()
            .endswith(
                ".crdownload"
            )
        ]

        if partials:

            partials.sort(

                key=lambda p:
                    p.stat().st_size
                    if p.exists()
                    else 0,

                reverse=True,
            )

            partial = (
                partials[0]
            )

            try:

                size = (
                    partial.stat()
                    .st_size
                )

                print(
                    "\r   下载中："
                    f"{partial.name} "
                    f"{size} bytes",
                    end="",
                    flush=True,
                )

            except Exception:

                pass

        # ====================================================
        # 下载完成
        # ====================================================

        completed = [

            path

            for path
            in new_files

            if (
                not path.name
                .lower()
                .endswith(
                    ".crdownload"
                )
                and
                not path.name
                .lower()
                .endswith(
                    ".tmp"
                )
            )
        ]

        if completed:

            completed.sort(

                key=lambda p:
                    p.stat().st_size
                    if p.exists()
                    else 0,

                reverse=True,
            )

            candidate = (
                completed[0]
            )

            try:

                size = (
                    candidate.stat()
                    .st_size
                )

            except Exception:

                await asyncio.sleep(
                    1
                )

                continue

            if (
                candidate == last_path
                and
                size == last_size
            ):

                stable_count += 1

            else:

                stable_count = 0

            last_path = candidate

            last_size = size

            if stable_count >= 3:

                print()

                return candidate

        await asyncio.sleep(
            1
        )

    print()

    return None


# ============================================================
# 14. Tab helper
# ============================================================

def tab_identity(
    tab,
):

    target_id = getattr(
        tab,
        "_target_id",
        None,
    )

    if target_id:

        return target_id

    target_id = getattr(
        tab,
        "target_id",
        None,
    )

    if target_id:

        return target_id

    return id(
        tab
    )


async def get_new_tabs(
    browser,
    old_ids,
):

    tabs = (
        await browser
        .get_opened_tabs()
    )

    return [

        tab

        for tab
        in tabs

        if (
            tab_identity(
                tab
            )
            not in old_ids
        )
    ]


async def close_new_blank_tabs(
    browser,
    old_ids,
):

    try:

        tabs = (
            await get_new_tabs(
                browser,
                old_ids,
            )
        )

        for tab in tabs:

            try:

                url = (
                    await tab.current_url
                )

                if (
                    not url
                    or
                    url == "about:blank"
                ):

                    await tab.close()

            except Exception:

                pass

    except Exception:

        pass


# ============================================================
# 15. DOI navigation
# ============================================================

async def navigate_doi(
    tab,
    doi,
):

    doi_url = (
        f"https://doi.org/{doi}"
    )

    publisher = (
        publisher_hint_from_doi(
            doi
        )
    )

    print()
    print(
        "Publisher hint:",
        publisher,
    )

    # ========================================================
    # ACS
    # ========================================================

    if publisher == PUBLISHER_ACS:

        try:

            async with (
                tab
                .expect_and_bypass_cloudflare_captcha(
                    time_to_wait_captcha=
                        CLOUDFLARE_TIMEOUT
                )
            ):

                await tab.go_to(
                    doi_url
                )

        except Exception as exc:

            # helper 报错有时页面已经成功
            print(
                "⚠️ Cloudflare helper:",
                repr(exc),
            )

    # ========================================================
    # RSC / Wiley
    # ========================================================

    else:

        try:

            await tab.go_to(
                doi_url
            )

        except Exception as exc:

            print(
                "⚠️ DOI navigation:",
                repr(exc),
            )

    await asyncio.sleep(
        PAGE_SETTLE_TIME
    )


# ============================================================
# 16. Journal metadata
# ============================================================

async def get_meta_content(
    tab,
    selectors,
):

    for selector in selectors:

        element = await tab.query(
            selector,
            timeout=2,
            raise_exc=False,
        )

        if not element:
            continue

        value = (
            element
            .get_attribute(
                "content"
            )
        )

        if value:

            return value.strip()

    return None


async def detect_journal_name(
    tab,
    publisher,
    doi,
):

    # ========================================================
    # 优先网页 metadata
    # ========================================================

    journal = await get_meta_content(
        tab,
        [
            'meta[name="citation_journal_title"]',
            'meta[name="DC.Source"]',
            'meta[name="dc.Source"]',
            'meta[property="citation_journal_title"]',
        ],
    )

    if journal:

        return clean_path_component(
            journal
        )

    # ========================================================
    # ACS fallback
    # ========================================================

    if publisher == PUBLISHER_ACS:

        suffix = (
            doi.split(
                "/",
                1,
            )[-1]
        )

        code = (
            suffix.split(
                ".",
                1,
            )[0]
        )

        if code in ACS_DOI_JOURNAL_CODES:

            return (
                ACS_DOI_JOURNAL_CODES[
                    code
                ]
            )

        title = (
            await tab.title
        )

        parts = [

            item.strip()

            for item
            in title.split("|")
        ]

        if len(parts) >= 2:

            return parts[-2]

    # ========================================================
    # RSC fallback
    # ========================================================

    if publisher == PUBLISHER_RSC:

        current_url = (
            await tab.current_url
        )

        url_parts = [

            item.lower()

            for item
            in urlparse(
                current_url
            ).path.split("/")

            if item
        ]

        for item in url_parts:

            if item in RSC_JOURNAL_CODES:

                return (
                    RSC_JOURNAL_CODES[
                        item
                    ]
                )

        suffix = (
            doi.split(
                "/",
                1,
            )[-1]
        )

        match = re.match(
            r"[a-z]\d([a-z]{2})",
            suffix,
            flags=re.IGNORECASE,
        )

        if match:

            code = (
                match.group(1)
                .lower()
            )

            if code in RSC_JOURNAL_CODES:

                return (
                    RSC_JOURNAL_CODES[
                        code
                    ]
                )

    # ========================================================
    # Wiley fallback
    # ========================================================

    if publisher == PUBLISHER_WILEY:

        suffix = (
            doi.split(
                "/",
                1,
            )[-1]
        )

        code = (
            suffix.split(
                ".",
                1,
            )[0]
            .lower()
        )

        if code in WILEY_DOI_JOURNAL_CODES:

            return (
                WILEY_DOI_JOURNAL_CODES[
                    code
                ]
            )

    return "Unknown Journal"


# ============================================================
# 17. 文件后缀判断
# ============================================================

CONTENT_TYPE_EXTENSIONS = {

    "application/pdf":
        ".pdf",

    "application/zip":
        ".zip",

    "application/x-zip-compressed":
        ".zip",

    "application/msword":
        ".doc",

    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        ".docx",

    "application/vnd.ms-excel":
        ".xls",

    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        ".xlsx",

    "text/csv":
        ".csv",

    "text/plain":
        ".txt",

    "chemical/x-cif":
        ".cif",

    "application/cif":
        ".cif",

    "video/mp4":
        ".mp4",

    "video/mpeg":
        ".mpg",

    "image/png":
        ".png",

    "image/jpeg":
        ".jpg",

    "image/tiff":
        ".tif",
}


URL_ROUTE_EXTENSIONS = {

    "/pdf/":
        ".pdf",

    "/cif/":
        ".cif",

    "/docx/":
        ".docx",

    "/doc/":
        ".doc",

    "/zip/":
        ".zip",

    "/xlsx/":
        ".xlsx",

    "/xls/":
        ".xls",

    "/csv/":
        ".csv",

    "/mp4/":
        ".mp4",

    "/mpg/":
        ".mpg",
}


def infer_extension(
    url,
    text="",
):
    """
    主要通过 URL 判断。

    Wiley SI 示例：

    ?file=anie74111-sup-0001-SuppMat.docx
    """

    parsed = urlparse(
        url
    )

    query = parse_qs(
        parsed.query
    )

    # ========================================================
    # Wiley query file=
    # ========================================================

    for key in (
        "file",
        "filename",
    ):

        values = query.get(
            key
        )

        if values:

            suffix = (
                Path(
                    values[0]
                ).suffix
            )

            if suffix:

                return suffix.lower()

    # ========================================================
    # URL filename suffix
    # ========================================================

    path = (
        parsed.path
        .lower()
    )

    suffix = (
        Path(
            path.rstrip("/")
        ).suffix
    )

    if (
        suffix
        and
        len(suffix) <= 10
    ):

        return suffix.lower()

    # ========================================================
    # RSC/ACS route
    # ========================================================

    for route, extension in (
        URL_ROUTE_EXTENSIONS.items()
    ):

        if route in path:

            return extension

    # ========================================================
    # link text fallback
    # ========================================================

    lower_text = (
        text.lower()
    )

    for extension in (
        ".pdf",
        ".docx",
        ".doc",
        ".mp4",
        ".cif",
        ".zip",
        ".xlsx",
        ".xls",
        ".csv",
        ".txt",
    ):

        if extension in lower_text:

            return extension

    return ".bin"


# ============================================================
# 18. 通用文件验证
# ============================================================

def validate_file(
    path,
    expect_pdf=False,
):

    result = {

        "valid":
            False,

        "size":
            None,

        "sha256":
            None,

        "reason":
            None,
    }

    if not path.exists():

        result[
            "reason"
        ] = "file_not_found"

        return result

    size = (
        path.stat()
        .st_size
    )

    result[
        "size"
    ] = size

    if size <= 0:

        result[
            "reason"
        ] = "empty_file"

        return result

    result[
        "sha256"
    ] = (
        sha256_file(
            path
        )
    )

    is_pdf = (
        expect_pdf
        or
        path.suffix.lower()
        ==
        ".pdf"
    )

    if is_pdf:

        with path.open(
            "rb"
        ) as file:

            head = file.read(
                64
            )

            file.seek(
                max(
                    0,
                    size - 4096,
                )
            )

            tail = file.read()

        if not head.startswith(
            b"%PDF"
        ):

            result[
                "reason"
            ] = "missing_pdf_header"

            return result

        if b"%%EOF" not in tail:

            result[
                "reason"
            ] = "missing_pdf_eof"

            return result

        if (
            b"\xef\xbf\xbd"
            in head
        ):

            result[
                "reason"
            ] = (
                "binary_reencoding_detected"
            )

            return result

    result[
        "valid"
    ] = True

    result[
        "reason"
    ] = "ok"

    return result


def existing_si_is_valid(
    path,
):

    if not path.exists():

        return False

    if not path.is_file():

        return False

    if (
        path.stat()
        .st_size
        <= 0
    ):

        return False

    if (
        path.suffix.lower()
        ==
        ".pdf"
    ):

        return existing_pdf_is_valid(
            path
        )

    return True


# ============================================================
# 19. Blob 下载
# ============================================================
#
# 用于：
#
# ACS Paper
# ACS SI
# RSC SI
# Wiley SI
#
# 注意：
#
# 不用于 RSC paper
# 不用于 Wiley paper
# ============================================================

async def blob_download_to_target(
    tab,
    url,
    target_path,
):

    target_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if target_path.exists():

        target_path.unlink()

    clear_browser_download_dir()

    js_url = json.dumps(
        url
    )

    js_filename = json.dumps(
        target_path.name
    )

    script = f"""
    (async () => {{

        const targetUrl =
            {js_url};

        const filename =
            {js_filename};

        const response =
            await fetch(
                targetUrl,
                {{
                    method:
                        "GET",

                    credentials:
                        "include",

                    redirect:
                        "follow",

                    cache:
                        "no-store"
                }}
            );

        if (!response.ok) {{

            throw new Error(
                "HTTP "
                +
                response.status
                +
                " "
                +
                response.statusText
            );
        }}

        const blob =
            await response.blob();

        if (
            !blob
            ||
            blob.size === 0
        ) {{

            throw new Error(
                "Empty Blob"
            );
        }}

        const objectUrl =
            URL.createObjectURL(
                blob
            );

        const a =
            document.createElement(
                "a"
            );

        a.href =
            objectUrl;

        a.download =
            filename;

        a.style.display =
            "none";

        document.body.appendChild(
            a
        );

        a.click();

        a.remove();

        setTimeout(
            () => {{
                URL.revokeObjectURL(
                    objectUrl
                );
            }},
            30000
        );

        return {{
            ok: true,
            size: blob.size,
            type: blob.type
        }};

    }})()
    """

    print()
    print(
        "⬇ Blob download"
    )

    print(
        url
    )

    async with tab.expect_download(

        keep_file_at=
            BROWSER_DOWNLOAD_DIR,

        timeout=
            DOWNLOAD_TIMEOUT,

    ) as download:

        await tab.execute_script(

            script,

            await_promise=True,

            return_by_value=True,

            user_gesture=True,

            timeout=
                DOWNLOAD_TIMEOUT
                * 1000,
        )

        data = (
            await download.read_bytes()
        )

        source = Path(
            download.file_path
        )

    if not source.exists():

        raise RuntimeError(
            "Blob download file missing"
        )

    if target_path.exists():

        target_path.unlink()

    shutil.move(
        str(source),
        str(target_path),
    )

    print(
        f"   ✓ {len(data)} bytes"
    )

    print(
        f"   → {target_path}"
    )

    return target_path


# ============================================================
# 20. ACS Paper
# ============================================================

async def acs_get_paper_url(
    tab,
):

    selectors = [

        (
            '//a['
            'contains('
            'normalize-space(.),'
            '"Open PDF"'
            ')'
            ']'
        ),

        (
            'a['
            'href*="/article-pdf/"'
            ']'
        ),
    ]

    for selector in selectors:

        element = await tab.query(
            selector,
            timeout=
                ELEMENT_TIMEOUT,
            raise_exc=False,
        )

        if not element:

            continue

        href = (
            element
            .get_attribute(
                "href"
            )
        )

        if href:

            return urljoin(
                await tab.current_url,
                href,
            )

    return None


async def download_acs_paper(
    tab,
    doi,
    paper_dir,
):

    paper_url = (
        await acs_get_paper_url(
            tab
        )
    )

    if not paper_url:

        raise RuntimeError(
            "ACS paper link not found"
        )

    target = (
        paper_dir
        /
        (
            doi_to_filename(
                doi
            )
            +
            ".pdf"
        )
    )

    print()
    print(
        "ACS Paper URL:"
    )

    print(
        paper_url
    )

    downloaded = (
        await blob_download_to_target(
            tab,
            paper_url,
            target,
        )
    )

    validation = (
        validate_file(
            downloaded,
            expect_pdf=True,
        )
    )

    if not validation[
        "valid"
    ]:

        raise RuntimeError(
            "ACS paper validation failed: "
            +
            str(
                validation[
                    "reason"
                ]
            )
        )

    return {

        "url":
            paper_url,

        "file":
            str(
                target
            ),

        "download_method":
            "acs_fetch_blob",

        "validation":
            validation,
    }


# ============================================================
# 21. ACS SI
# ============================================================

async def acs_get_si_links(
    tab,
):

    elements = await tab.query(

        (
            'a['
            'data-doctype="dataSupplementDoc"'
            ']'
        ),

        timeout=
            ELEMENT_TIMEOUT,

        find_all=True,

        raise_exc=False,
    )

    if not elements:

        elements = await tab.query(

            (
                'a['
                'href*="/article-supplement/"'
                ']'
            ),

            timeout=10,

            find_all=True,

            raise_exc=False,
        )

    if not elements:

        return []

    current_url = (
        await tab.current_url
    )

    result = []

    seen = set()

    for element in elements:

        href = (
            element
            .get_attribute(
                "href"
            )
        )

        if not href:

            continue

        url = urljoin(
            current_url,
            href,
        )

        if url in seen:

            continue

        seen.add(
            url
        )

        try:

            text = (
                await element.text
            )

        except Exception:

            text = ""

        result.append(
            {
                "url":
                    url,

                "text":
                    text,
            }
        )

    return result


# ============================================================
# 22. RSC Paper
# ============================================================

async def wait_for_rsc_pdf_element(
    tab,
):

    print()
    print(
        "等待 RSC 正文 PDF 元素，"
        f"最长 {RSC_RUNTIME_TIMEOUT} 秒..."
    )

    element = await tab.query(

        (
            'a['
            'data-doctype="contentPdf"'
            ']['
            'href*="/article-pdf/"'
            ']'
        ),

        timeout=
            RSC_RUNTIME_TIMEOUT,

        raise_exc=False,
    )

    if element:

        return element

    return await tab.query(

        (
            'a.article-pdfLink'
            '[href]'
        ),

        timeout=15,

        raise_exc=False,
    )


async def download_rsc_paper(
    browser,
    tab,
    doi,
    paper_dir,
):

    element = (
        await wait_for_rsc_pdf_element(
            tab
        )
    )

    if not element:

        raise RuntimeError(
            "RSC contentPdf element not found"
        )

    href = (
        element.get_attribute(
            "href"
        )
    )

    if not href:

        raise RuntimeError(
            "RSC PDF href missing"
        )

    if href.startswith("/"):

        pdf_url = (
            "https://pubs.rsc.org"
            +
            href
        )

    else:

        pdf_url = urljoin(
            await tab.current_url,
            href,
        )

    print()
    print(
        "RSC PDF URL:"
    )

    print(
        pdf_url
    )

    # ========================================================
    # RSC：
    # 已实测必须 real click
    # ========================================================

    clear_browser_download_dir()

    before = (
        staging_snapshot()
    )

    old_tabs = (
        await browser
        .get_opened_tabs()
    )

    old_ids = {

        tab_identity(
            item
        )

        for item
        in old_tabs
    }

    try:

        await element.scroll_into_view()

    except Exception:

        pass

    try:

        await element.click(
            humanize=True
        )

    except Exception as exc:

        print(
            "RSC human click failed:",
            repr(exc),
        )

        await element.execute_script(
            "this.click()",
            user_gesture=True,
        )

    downloaded = (
        await wait_for_native_download(
            before,
            timeout=
                RSC_RUNTIME_TIMEOUT,
        )
    )

    await close_new_blank_tabs(
        browser,
        old_ids,
    )

    if not downloaded:

        raise TimeoutError(
            "RSC paper download timeout"
        )

    target = (
        paper_dir
        /
        (
            doi_to_filename(
                doi
            )
            +
            ".pdf"
        )
    )

    if target.exists():

        target.unlink()

    shutil.move(
        str(downloaded),
        str(target),
    )

    validation = (
        validate_file(
            target,
            expect_pdf=True,
        )
    )

    if not validation[
        "valid"
    ]:

        raise RuntimeError(
            "RSC PDF validation failed: "
            +
            str(
                validation[
                    "reason"
                ]
            )
        )

    return {

        "url":
            pdf_url,

        "file":
            str(
                target
            ),

        "download_method":
            "rsc_real_click",

        "validation":
            validation,
    }


# ============================================================
# 23. RSC SI
# ============================================================

async def rsc_get_si_links(
    tab,
):

    elements = await tab.query(

        (
            'a['
            'href*="/article-supplement/"'
            ']'
        ),

        timeout=
            ELEMENT_TIMEOUT,

        find_all=True,

        raise_exc=False,
    )

    if not elements:

        return []

    result = []

    seen = set()

    for element in elements:

        href = (
            element
            .get_attribute(
                "href"
            )
        )

        if not href:

            continue

        if href.startswith("/"):

            url = (
                "https://pubs.rsc.org"
                +
                href
            )

        else:

            url = urljoin(
                await tab.current_url,
                href,
            )

        if url in seen:

            continue

        seen.add(
            url
        )

        try:

            text = (
                await element.text
            )

        except Exception:

            text = ""

        result.append(
            {
                "url":
                    url,

                "text":
                    text,
            }
        )

    return result


# ============================================================
# 24. Wiley ePDF
# ============================================================

async def wait_for_wiley_epdf_element(
    tab,
):

    print()
    print(
        "等待 Wiley ePDF 元素，"
        f"最长 {WILEY_RUNTIME_TIMEOUT} 秒..."
    )

    selectors = [

        (
            'a.pdf-download'
            '[href*="/doi/epdf/"]'
        ),

        (
            'a[title="ePDF"]'
            '[href*="/doi/epdf/"]'
        ),

        (
            'a.coolBar__ctrl'
            '[href*="/doi/epdf/"]'
        ),

        (
            'a[href*="/doi/epdf/"]'
        ),
    ]

    for index, selector in enumerate(
        selectors
    ):

        element = await tab.query(

            selector,

            timeout=(
                WILEY_RUNTIME_TIMEOUT
                if index == 0
                else 10
            ),

            raise_exc=False,
        )

        if element:

            return element

    return None


# ============================================================
# 25. 找 Wiley Reader
# ============================================================

async def wait_for_wiley_reader(
    browser,
    article_tab,
    old_ids,
):

    print()
    print(
        "等待 Wiley Reader..."
    )

    loop = (
        asyncio.get_running_loop()
    )

    deadline = (
        loop.time()
        +
        WILEY_RUNTIME_TIMEOUT
    )

    while (
        loop.time()
        <
        deadline
    ):

        try:

            tabs = (
                await browser
                .get_opened_tabs()
            )

        except Exception:

            tabs = []

        candidates = [

            candidate

            for candidate
            in tabs

            if (
                tab_identity(
                    candidate
                )
                not in old_ids
            )
        ]

        # 同 Tab Reader fallback
        candidates.append(
            article_tab
        )

        seen = set()

        for candidate in candidates:

            identity = (
                tab_identity(
                    candidate
                )
            )

            if identity in seen:
                continue

            seen.add(
                identity
            )

            try:

                button = await candidate.query(

                    (
                        'button#new-download-btn'
                        '[aria-label="Download"]'
                    ),

                    timeout=1,

                    raise_exc=False,
                )

            except Exception:

                button = None

            if button:

                try:

                    reader_url = (
                        await candidate.current_url
                    )

                except Exception:

                    reader_url = "<unknown>"

                print()
                print(
                    "✅ Wiley Reader 找到"
                )

                print(
                    "Reader URL:"
                )

                print(
                    reader_url
                )

                return (
                    candidate,
                    button,
                )

        await asyncio.sleep(
            0.5
        )

    return (
        None,
        None,
    )


# ============================================================
# 26. Wiley Reader PDF 下载
# ============================================================

async def wiley_reader_download_pdf(
    browser,
    reader_tab,
    download_button,
    doi,
    paper_dir,
):
    """
    ★ 已通过实际测试确定正确流程：

    Download button：
        human click 成功

    PDF option：
        visibility:hidden
        0×0
        human click 必然 ElementNotVisible

    所以 PDF option 不再尝试 human click。

    固定：

        await pdf_link.execute_script(
            "this.click()",
            user_gesture=True
        )
    """

    print()
    print(
        "=" * 72
    )

    print(
        "WILEY READER DOWNLOAD"
    )

    print(
        "=" * 72
    )

    # ========================================================
    # STEP 1 - Download button
    # ========================================================

    print()
    print(
        "STEP 1 - 点击 Download 按钮"
    )

    try:

        await download_button.scroll_into_view()

    except Exception:

        pass

    try:

        await download_button.click(
            humanize=True
        )

        print(
            "✅ Download human click"
        )

    except Exception as exc:

        print(
            "Download human click failed:"
        )

        print(
            repr(exc)
        )

        # 新 API：
        # WebElement.execute_script
        await download_button.execute_script(
            "this.click()",
            user_gesture=True,
        )

        print(
            "✅ Download JS click fallback"
        )

    # 给 dropdown 展开时间
    await asyncio.sleep(
        1
    )

    # ========================================================
    # STEP 2 - PDF option
    # ========================================================

    print()
    print(
        "STEP 2 - 寻找 PDF option"
    )

    pdf_link = await reader_tab.query(

        (
            '#download-popup '
            'a['
            'data-download-files-key="pdf"'
            ']'
        ),

        timeout=30,

        raise_exc=False,
    )

    if not pdf_link:

        pdf_link = await reader_tab.query(

            (
                'a.download.list-button'
                '[data-single-download="true"]'
                '[href*="/doi/pdfdirect/"]'
            ),

            timeout=15,

            raise_exc=False,
        )

    if not pdf_link:

        pdf_link = await reader_tab.query(

            (
                'a['
                'href*="/doi/pdfdirect/"'
                ']'
            ),

            timeout=10,

            raise_exc=False,
        )

    if not pdf_link:

        raise RuntimeError(
            "Wiley PDF option not found"
        )

    href = (
        pdf_link
        .get_attribute(
            "href"
        )
    )

    try:

        text = (
            await pdf_link.text
        )

    except Exception:

        text = ""

    print()
    print(
        "✅ Wiley PDF option found"
    )

    print(
        "text:",
        repr(text),
    )

    print(
        "href:",
        href,
    )

    if not href:

        raise RuntimeError(
            "Wiley pdfdirect href missing"
        )

    reader_url = (
        await reader_tab.current_url
    )

    pdf_url = urljoin(
        reader_url,
        href,
    )

    print()
    print(
        "Final Wiley PDF URL:"
    )

    print(
        pdf_url
    )

    # ========================================================
    # STEP 3
    #
    # ★ 已验证成功的 JS CLICK
    # ========================================================

    clear_browser_download_dir()

    before = (
        staging_snapshot()
    )

    old_tabs = (
        await browser
        .get_opened_tabs()
    )

    old_ids = {

        tab_identity(
            item
        )

        for item
        in old_tabs
    }

    print()
    print(
        "STEP 3 - WebElement JS click PDF"
    )

    # --------------------------------------------------------
    # 不再 human click！
    #
    # 实测该元素：
    #
    # visibility: hidden
    # width: 0
    # height: 0
    #
    # 但 this.click() 成功触发 PDF。
    # --------------------------------------------------------

    await pdf_link.execute_script(
        "this.click()",
        user_gesture=True,
    )

    print(
        "✅ PDF WebElement JS click executed"
    )

    downloaded = (
        await wait_for_native_download(
            before,
            timeout=
                WILEY_PDF_DOWNLOAD_TIMEOUT,
        )
    )

    await close_new_blank_tabs(
        browser,
        old_ids,
    )

    # ========================================================
    # 极端 fallback：
    # 直接访问 pdfdirect
    # ========================================================

    if not downloaded:

        print()
        print(
            "⚠️ JS click 未检测到下载"
        )

        print(
            "→ direct pdfdirect fallback"
        )

        clear_browser_download_dir()

        before = (
            staging_snapshot()
        )

        direct_tab = (
            await browser.new_tab()
        )

        try:

            await direct_tab.go_to(
                pdf_url
            )

        except Exception as exc:

            print(
                "pdfdirect go_to warning:",
                repr(exc),
            )

        downloaded = (
            await wait_for_native_download(
                before,
                timeout=
                    WILEY_PDF_DOWNLOAD_TIMEOUT,
            )
        )

        try:

            await direct_tab.close()

        except Exception:

            pass

    if not downloaded:

        raise TimeoutError(
            "Wiley PDF download timeout"
        )

    # ========================================================
    # 统一正文命名
    # ========================================================

    target = (
        paper_dir
        /
        (
            doi_to_filename(
                doi
            )
            +
            ".pdf"
        )
    )

    if target.exists():

        target.unlink()

    shutil.move(
        str(downloaded),
        str(target),
    )

    # ========================================================
    # Validation
    # ========================================================

    validation = (
        validate_file(
            target,
            expect_pdf=True,
        )
    )

    if not validation[
        "valid"
    ]:

        raise RuntimeError(
            "Wiley PDF validation failed: "
            +
            str(
                validation[
                    "reason"
                ]
            )
        )

    print()
    print(
        "✅ Wiley正文下载成功"
    )

    print(
        target
    )

    print(
        "size:",
        validation[
            "size"
        ],
    )

    print(
        "SHA256:",
        validation[
            "sha256"
        ],
    )

    return {

        "url":
            pdf_url,

        "file":
            str(
                target
            ),

        "download_method":
            (
                "wiley_reader_"
                "webelement_js_click"
            ),

        "validation":
            validation,
    }


# ============================================================
# 27. Wiley Paper
# ============================================================

async def download_wiley_paper(
    browser,
    article_tab,
    doi,
    paper_dir,
):

    print()
    print(
        "=" * 72
    )

    print(
        "WILEY PAPER"
    )

    print(
        "=" * 72
    )

    # ========================================================
    # Article → ePDF
    # ========================================================

    epdf = (
        await wait_for_wiley_epdf_element(
            article_tab
        )
    )

    if not epdf:

        raise RuntimeError(
            "Wiley ePDF element not found"
        )

    href = (
        epdf.get_attribute(
            "href"
        )
    )

    try:

        text = (
            await epdf.text
        )

    except Exception:

        text = ""

    print()
    print(
        "✅ Wiley ePDF found"
    )

    print(
        "text:",
        repr(text),
    )

    print(
        "href:",
        href,
    )

    # ========================================================
    # 保存点击前 tabs
    # ========================================================

    old_tabs = (
        await browser
        .get_opened_tabs()
    )

    old_ids = {

        tab_identity(
            item
        )

        for item
        in old_tabs
    }

    # ========================================================
    # ePDF click
    #
    # 实测：
    # human click 可能 ElementNotVisible
    #
    # 因此：
    # human → JS fallback
    # ========================================================

    print()
    print(
        "点击 Wiley ePDF..."
    )

    try:

        await epdf.scroll_into_view()

    except Exception:

        pass

    try:

        await epdf.click(
            humanize=True
        )

        print(
            "✅ ePDF human click"
        )

    except Exception as exc:

        print(
            "ePDF human click failed:"
        )

        print(
            repr(exc)
        )

        await epdf.execute_script(
            "this.click()",
            user_gesture=True,
        )

        print(
            "✅ ePDF WebElement JS click"
        )

    # ========================================================
    # 找 Reader
    # ========================================================

    (
        reader_tab,
        download_button,

    ) = await wait_for_wiley_reader(

        browser,
        article_tab,
        old_ids,
    )

    if not reader_tab:

        raise RuntimeError(
            "Wiley Reader not found"
        )

    # ========================================================
    # Reader → PDF
    # ========================================================

    result = (
        await wiley_reader_download_pdf(
            browser,
            reader_tab,
            download_button,
            doi,
            paper_dir,
        )
    )

    # ========================================================
    # 如果 Reader 是新 Tab：
    #
    # 关闭 Reader。
    #
    # Article Tab 仍停留原文章。
    #
    # 如果 Reader == article_tab：
    #
    # 不关。
    # 后面 restore_wiley_article_page_for_si()
    # 会 history.back()
    # ========================================================

    if (
        tab_identity(
            reader_tab
        )
        !=
        tab_identity(
            article_tab
        )
    ):

        try:

            await reader_tab.close()

            print()
            print(
                "✅ Wiley Reader 新 Tab 已关闭"
            )

        except Exception:

            pass

    return result


# ============================================================
# 28. ★★★ Wiley 回到 Article 页面 ★★★
# ============================================================

async def restore_wiley_article_page_for_si(
    article_tab,
    original_article_url,
):
    """
    这是 Wiley 正文 → SI 之间的强制步骤。

    场景 1：
        ePDF 新 Tab 打开 Reader

    此时 article_tab 仍是文章页：
        → 直接继续。


    场景 2：
        ePDF 在 article_tab 内打开 Reader

    此时：
        → history.back()
        → 等文章页面恢复。


    场景 3：
        history.back() 不成功

    此时：
        → 直接重新访问保存的 original_article_url。


    最终必须确认：

        ePDF element

    或：

        table.support-info__table

    已重新出现。
    """

    print()
    print(
        "=" * 72
    )

    print(
        "WILEY RESTORE ARTICLE FOR SI"
    )

    print(
        "=" * 72
    )

    try:

        current_url = (
            await article_tab.current_url
        )

    except Exception:

        current_url = ""

    print()
    print(
        "Current URL:"
    )

    print(
        current_url
    )

    # ========================================================
    # 第一检查：
    # 当前是不是已经 Article？
    # ========================================================

    epdf_marker = await article_tab.query(

        (
            'a['
            'href*="/doi/epdf/"'
            ']'
        ),

        timeout=2,

        raise_exc=False,
    )

    si_table = await article_tab.query(

        "table.support-info__table",

        timeout=2,

        raise_exc=False,
    )

    if (
        epdf_marker
        or
        si_table
    ):

        print()
        print(
            "✅ Wiley Article Tab 本来就还在"
        )

        print(
            "无需 history.back()"
        )

        return True

    # ========================================================
    # 第二方案：
    # history.back()
    # ========================================================

    print()
    print(
        "当前处于 Reader。"
    )

    print(
        "→ history.back() 返回 Article..."
    )

    try:

        await article_tab.execute_script(
            "history.back();",
            user_gesture=True,
        )

    except Exception as exc:

        # navigation 导致 context 被销毁也可能抛异常
        print(
            "history.back warning:",
            repr(exc),
        )

    # ========================================================
    # 等 30 秒看 Article 是否恢复
    # ========================================================

    loop = (
        asyncio.get_running_loop()
    )

    deadline = (
        loop.time()
        + 30
    )

    while (
        loop.time()
        <
        deadline
    ):

        try:

            epdf_marker = (
                await article_tab.query(

                    (
                        'a['
                        'href*="/doi/epdf/"'
                        ']'
                    ),

                    timeout=1,

                    raise_exc=False,
                )
            )

            si_table = (
                await article_tab.query(

                    "table.support-info__table",

                    timeout=1,

                    raise_exc=False,
                )
            )

            if (
                epdf_marker
                or
                si_table
            ):

                try:

                    restored_url = (
                        await article_tab.current_url
                    )

                except Exception:

                    restored_url = "<unknown>"

                print()
                print(
                    "✅ history.back() 恢复成功"
                )

                print(
                    "Article URL:"
                )

                print(
                    restored_url
                )

                return True

        except Exception:

            pass

        await asyncio.sleep(
            0.5
        )

    # ========================================================
    # 第三方案：
    #
    # 强制重新访问原 Article URL
    # ========================================================

    print()
    print(
        "⚠️ history.back() 未恢复 Article"
    )

    print(
        "→ 重新访问原文章 URL："
    )

    print(
        original_article_url
    )

    try:

        await article_tab.go_to(
            original_article_url
        )

    except Exception as exc:

        print(
            "restore go_to warning:",
            repr(exc),
        )

    await asyncio.sleep(
        PAGE_SETTLE_TIME
    )

    # ========================================================
    # 最终确认 Article DOM
    # ========================================================

    article_marker = (
        await article_tab.query(

            (
                'a['
                'href*="/doi/epdf/"'
                ']'
            ),

            timeout=
                WILEY_RUNTIME_TIMEOUT,

            raise_exc=False,
        )
    )

    if not article_marker:

        raise RuntimeError(
            "Wiley Article 页面恢复失败"
        )

    print()
    print(
        "✅ 已重新访问 Wiley Article"
    )

    try:

        restored_url = (
            await article_tab.current_url
        )

        print(
            "Article URL:"
        )

        print(
            restored_url
        )

    except Exception:

        pass

    return True


# ============================================================
# 29. Wiley SI links
# ============================================================

async def wiley_get_si_links(
    article_tab,
):
    """
    ★ 只允许在恢复后的 Article 页面调用。

    用户明确要求：

    <span>Supporting Information</span>

    对应：

    <table class="support-info__table ...">

    table 内所有：

        a[href]

    全部作为 SI。
    """

    print()
    print(
        "Searching Wiley Supporting Information..."
    )

    try:

        current_url = (
            await article_tab.current_url
        )

    except Exception:

        current_url = "<unknown>"

    print()
    print(
        "SI Search Page:"
    )

    print(
        current_url
    )

    # ========================================================
    # Supporting Information 标题
    # ========================================================

    heading = await article_tab.query(

        (
            '//span['
            'normalize-space(.)='
            '"Supporting Information"'
            ']'
        ),

        timeout=20,

        raise_exc=False,
    )

    if heading:

        print(
            "✅ Supporting Information 标题找到"
        )

    # ========================================================
    # SI Table
    # ========================================================

    table = await article_tab.query(

        "table.support-info__table",

        timeout=
            WILEY_RUNTIME_TIMEOUT,

        raise_exc=False,
    )

    if not table:

        print(
            "ℹ️ 该 Wiley 文章未找到 "
            "support-info__table"
        )

        return []

    print(
        "✅ support-info__table 找到"
    )

    # ========================================================
    # ★ table 内所有 a[href]
    # ========================================================

    elements = await article_tab.query(

        (
            "table.support-info__table "
            "a[href]"
        ),

        timeout=30,

        find_all=True,

        raise_exc=False,
    )

    if not elements:

        return []

    base_url = (
        await article_tab.current_url
    )

    result = []

    seen = set()

    for index, element in enumerate(
        elements,
        start=1,
    ):

        href = (
            element
            .get_attribute(
                "href"
            )
        )

        if not href:

            continue

        url = urljoin(
            base_url,
            href,
        )

        if url in seen:

            continue

        seen.add(
            url
        )

        try:

            text = (
                await element.text
            )

        except Exception:

            text = ""

        print()
        print(
            f"Wiley SI link #{index}"
        )

        print(
            "text:",
            repr(text),
        )

        print(
            "href:",
            href,
        )

        result.append(
            {
                "url":
                    url,

                "text":
                    text,
            }
        )

    return result


# ============================================================
# 30. SI 通用 Blob 下载
# ============================================================
#
# ACS SI
# RSC SI
# Wiley SI
#
# Wiley 已经回到 Article 页之后，
# 先把所有 SI URL 提取出来，
# 再逐个 Blob 下载。
#
# 这样不会因为 SI click 再次破坏 Article 页面状态。
# ============================================================

async def download_si_files_blob(
    tab,
    doi,
    links,
    si_dir,
):

    results = []

    doi_name = (
        doi_to_filename(
            doi
        )
    )

    for index, link in enumerate(
        links,
        start=1,
    ):

        url = (
            link[
                "url"
            ]
        )

        text = (
            link.get(
                "text"
            )
            or ""
        )

        extension = (
            infer_extension(
                url,
                text,
            )
        )

        target = (
            si_dir
            /
            (
                f"{doi_name}"
                f"_si_"
                f"{index:03d}"
                f"{extension}"
            )
        )

        print()
        print(
            "=" * 72
        )

        print(
            f"SI #{index}"
        )

        print(
            "=" * 72
        )

        print(
            "URL:"
        )

        print(
            url
        )

        print(
            "Target:"
        )

        print(
            target
        )

        # ====================================================
        # SI 文件级重复
        # ========================================================

        if existing_si_is_valid(
            target
        ):

            print(
                "SI已存在 跳过："
                f"{target.name}"
            )

            results.append(
                {
                    "index":
                        index,

                    "source_url":
                        url,

                    "file":
                        str(
                            target
                        ),

                    "existing":
                        True,

                    "validation":
                        {
                            "valid":
                                True,

                            "size":
                                target.stat()
                                .st_size,

                            "sha256":
                                sha256_file(
                                    target
                                ),

                            "reason":
                                "already_exists",
                        },
                }
            )

            continue

        # ====================================================
        # Blob
        # ========================================================

        try:

            downloaded = (
                await blob_download_to_target(
                    tab,
                    url,
                    target,
                )
            )

            validation = (
                validate_file(
                    downloaded,
                    expect_pdf=(
                        extension.lower()
                        ==
                        ".pdf"
                    ),
                )
            )

            if not validation[
                "valid"
            ]:

                raise RuntimeError(
                    validation[
                        "reason"
                    ]
                )

            print(
                f"✅ SI #{index}: "
                f"{target.name}"
            )

            results.append(
                {
                    "index":
                        index,

                    "source_url":
                        url,

                    "file":
                        str(
                            target
                        ),

                    "download_method":
                        "fetch_blob",

                    "validation":
                        validation,
                }
            )

        except Exception as exc:

            print(
                f"❌ SI #{index} failed:"
            )

            print(
                repr(exc)
            )

            results.append(
                {
                    "index":
                        index,

                    "source_url":
                        url,

                    "file":
                        str(
                            target
                        ),

                    "error":
                        repr(exc),
                }
            )

    return results


# ============================================================
# 31. Process one DOI
# ============================================================

async def process_one_doi(
    browser,
    article_tab,
    doi,
):

    print()
    print()
    print(
        "=" * 80
    )

    print(
        f"DOI: {doi}"
    )

    print(
        "=" * 80
    )

    # ========================================================
    # Article 页面
    # ========================================================

    await navigate_doi(
        article_tab,
        doi,
    )

    landing_url = (
        await article_tab.current_url
    )

    # ★ Wiley 后面需要这个 URL 回退 / 恢复
    original_article_url = (
        landing_url
    )

    title = (
        await article_tab.title
    )

    print()
    print(
        "Landing URL:"
    )

    print(
        landing_url
    )

    print()
    print(
        "Page title:"
    )

    print(
        title
    )

    # ========================================================
    # Publisher
    # ========================================================

    publisher = (
        classify_publisher(
            landing_url,
            doi,
        )
    )

    print()
    print(
        "Detected publisher:",
        publisher,
    )

    if publisher not in {

        PUBLISHER_ACS,

        PUBLISHER_RSC,

        PUBLISHER_WILEY,

    }:

        raise RuntimeError(
            f"Unsupported publisher: {publisher}"
        )

    # ========================================================
    # 等动态 DOM
    # ========================================================

    if publisher == PUBLISHER_RSC:

        await wait_for_rsc_pdf_element(
            article_tab
        )

    elif publisher == PUBLISHER_WILEY:

        await wait_for_wiley_epdf_element(
            article_tab
        )

    # ========================================================
    # Journal
    # ========================================================

    journal = (
        await detect_journal_name(
            article_tab,
            publisher,
            doi,
        )
    )

    print()
    print(
        "Journal:",
        journal,
    )

    (
        journal_dir,
        paper_dir,
        si_dir,

    ) = make_journal_directories(
        publisher,
        journal,
    )

    print(
        "Archive:"
    )

    print(
        journal_dir
    )

    # ========================================================
    # PAPER
    # ========================================================

    print()
    print(
        "--- PAPER ---"
    )

    try:

        # ----------------------------------------------------
        # ACS
        # ----------------------------------------------------

        if publisher == PUBLISHER_ACS:

            paper_result = (
                await download_acs_paper(
                    article_tab,
                    doi,
                    paper_dir,
                )
            )

        # ----------------------------------------------------
        # RSC
        # ----------------------------------------------------

        elif publisher == PUBLISHER_RSC:

            paper_result = (
                await download_rsc_paper(
                    browser,
                    article_tab,
                    doi,
                    paper_dir,
                )
            )

        # ----------------------------------------------------
        # Wiley
        # ----------------------------------------------------

        elif publisher == PUBLISHER_WILEY:

            paper_result = (
                await download_wiley_paper(
                    browser,
                    article_tab,
                    doi,
                    paper_dir,
                )
            )

        else:

            raise RuntimeError(
                "Unsupported publisher"
            )

        print()
        print(
            "✅ Paper downloaded"
        )

    except Exception as exc:

        print()
        print(
            "❌ Paper download failed:"
        )

        print(
            repr(exc)
        )

        paper_result = {

            "error":
                repr(exc)
        }

    # ========================================================
    # ★★★ WILEY 回 Article ★★★
    # ========================================================
    #
    # 这里无论正文成功还是失败，
    # 都尝试恢复 Article。
    #
    # 因为正文流程可能已经进入 ePDF Reader。
    # ========================================================

    if publisher == PUBLISHER_WILEY:

        print()
        print(
            "Wiley 正文阶段结束。"
        )

        print(
            "开始恢复 Article 页面，"
            "然后再下载 SI..."
        )

        await restore_wiley_article_page_for_si(

            article_tab,

            original_article_url,
        )

    # ========================================================
    # SI SEARCH
    # ========================================================

    print()
    print(
        "--- SUPPLEMENTARY INFORMATION ---"
    )

    try:

        if publisher == PUBLISHER_ACS:

            si_links = (
                await acs_get_si_links(
                    article_tab
                )
            )

        elif publisher == PUBLISHER_RSC:

            si_links = (
                await rsc_get_si_links(
                    article_tab
                )
            )

        elif publisher == PUBLISHER_WILEY:

            # ★ 这里只会在 Article 恢复后执行
            si_links = (
                await wiley_get_si_links(
                    article_tab
                )
            )

        else:

            si_links = []

    except Exception as exc:

        print(
            "SI link search failed:"
        )

        print(
            repr(exc)
        )

        si_links = []

    print()
    print(
        "Detected SI links:",
        len(
            si_links
        ),
    )

    # ========================================================
    # SI DOWNLOAD
    # ========================================================
    #
    # 三家现在全部可以走 Blob：
    #
    # ACS SI ✅
    # RSC SI ✅
    # Wiley SI ✅
    #
    # Wiley 在这里不再 click SI，
    # 避免再次破坏 Article 页面状态。
    # ========================================================

    si_results = (
        await download_si_files_blob(
            article_tab,
            doi,
            si_links,
            si_dir,
        )
    )

    successful_si = [

        item

        for item
        in si_results

        if (
            item.get(
                "validation"
            )
            and
            item[
                "validation"
            ].get(
                "valid"
            )
        )
    ]

    paper_success = bool(

        paper_result

        and

        paper_result.get(
            "validation",
            {},
        ).get(
            "valid"
        )
    )

    # ========================================================
    # Record
    # ========================================================

    record = {

        "doi":
            doi,

        "article_url":
            original_article_url,

        "page_title":
            title,

        "publisher":
            publisher,

        "publisher_full_name":
            PUBLISHER_FULL_NAMES.get(
                publisher
            ),

        "journal":
            journal,

        "archive_directory":
            str(
                journal_dir
            ),

        "paper":
            paper_result,

        "supporting_information":
            si_results,

        "summary":
            {

                "paper_success":
                    paper_success,

                "si_detected":
                    len(
                        si_results
                    ),

                "si_successful":
                    len(
                        successful_si
                    ),
            },
    }

    print()
    print(
        "--- RESULT ---"
    )

    print(
        "Paper success:",
        paper_success,
    )

    print(
        "SI detected:",
        len(
            si_results
        ),
    )

    print(
        "SI successful:",
        len(
            successful_si
        ),
    )

    return record


# ============================================================
# 32. Manifest
# ============================================================

def save_manifest(
    records,
):

    manifest = {

        "source":
            str(
                DOI_LIST_PATH
            ),

        "download_root":
            str(
                DOWNLOAD_ROOT
            ),

        "count":
            len(
                records
            ),

        "records":
            records,
    }

    MANIFEST_PATH.write_text(

        json.dumps(
            manifest,
            indent=2,
            ensure_ascii=False,
        ),

        encoding="utf-8",
    )

    print()
    print(
        "Manifest:"
    )

    print(
        MANIFEST_PATH
    )


# ============================================================
# 33. Edge Options
# ============================================================

def build_browser_options():

    options = (
        ChromiumOptions()
    )

    options.set_default_download_directory(
        str(
            BROWSER_DOWNLOAD_DIR
            .resolve()
        )
    )

    # 不弹 Save As
    options.prompt_for_download = (
        False
    )

    # 允许多个附件下载
    options.allow_automatic_downloads = (
        True
    )

    # RSC / Wiley pdfdirect
    #
    # PDF 进入 Chromium Download Manager
    # 不进入内置 PDF Viewer。
    options.open_pdf_externally = (
        True
    )

    return options


# ============================================================
# 34. MAIN
# ============================================================

async def main():

    clear_browser_download_dir()

    doi_list = (
        read_doi_list()
    )

    if not doi_list:

        print(
            "doi_list.txt 中没有有效 DOI"
        )

        return

    print()
    print(
        "=" * 80
    )

    print(
        "ACS + RSC + Wiley Batch Downloader"
    )

    print(
        "=" * 80
    )

    print()
    print(
        f"DOI count: "
        f"{len(doi_list)}"
    )

    print()

    for doi in doi_list:

        print(
            "-",
            doi,
        )

    # ========================================================
    # ★★★ DOI 全局重复检查 ★★★
    # ========================================================
    #
    # 此步骤发生在 Edge 启动之前。
    #
    # 有效正文已经存在：
    #
    #   不访问 DOI
    #   不打开网站
    #   不下载 paper
    #   不下载 SI
    #
    # ========================================================

    print()
    print(
        "=" * 80
    )

    print(
        "DOI重复检查"
    )

    print(
        "=" * 80
    )

    records = []

    pending = []

    duplicate_count = 0

    for doi in doi_list:

        existing = (
            find_existing_paper(
                doi
            )
        )

        # ====================================================
        # 重复
        # ====================================================

        if existing:

            duplicate_count += 1

            print()
            print(
                f"重复下载 跳过任务doi：{doi}"
            )

            print(
                f"已有正文：{existing}"
            )

            print(
                "不访问网页、不重新下载正文、不重新下载SI。"
            )

            records.append(
                {
                    "doi":
                        doi,

                    "skipped":
                        True,

                    "skip_reason":
                        "duplicate_download",

                    "existing_paper":
                        str(
                            existing
                        ),

                    "summary":
                        {

                            "paper_success":
                                True,

                            "si_detected":
                                None,

                            "si_successful":
                                None,
                        },
                }
            )

        # ====================================================
        # 未重复
        # ====================================================

        else:

            print()
            print(
                f"未重复 加入任务doi：{doi}"
            )

            pending.append(
                doi
            )

    print()
    print(
        "-" * 80
    )

    print(
        "重复 DOI:",
        duplicate_count,
    )

    print(
        "待处理 DOI:",
        len(
            pending
        ),
    )

    print(
        "-" * 80
    )

    # ========================================================
    # 全重复
    # ========================================================

    if not pending:

        save_manifest(
            records
        )

        print()
        print(
            "=" * 80
        )

        print(
            "所有 DOI 均重复。"
        )

        print(
            "Edge 未启动，没有进行任何网络访问。"
        )

        print(
            "=" * 80
        )

        return

    # ========================================================
    # Browser
    # ========================================================

    options = (
        build_browser_options()
    )

    async with Edge(
        options=options
    ) as browser:

        article_tab = (
            await browser.start()
        )

        print()
        print(
            "✅ Edge started"
        )

        # ====================================================
        # 只跑 pending
        # ====================================================

        for index, doi in enumerate(
            pending,
            start=1,
        ):

            print()
            print(
                f"[{index}/{len(pending)}]"
            )

            try:

                record = (
                    await process_one_doi(

                        browser,

                        article_tab,

                        doi,
                    )
                )

            except Exception as exc:

                print()
                print(
                    "❌ DOI processing failed:"
                )

                print(
                    repr(exc)
                )

                record = {

                    "doi":
                        doi,

                    "error":
                        repr(exc),

                    "summary":
                        {

                            "paper_success":
                                False,

                            "si_detected":
                                0,

                            "si_successful":
                                0,
                        },
                }

            records.append(
                record
            )

            # 每篇结束立即写 manifest
            save_manifest(
                records
            )

            # 清 staging
            clear_browser_download_dir()

            await asyncio.sleep(
                2
            )

    # ========================================================
    # Final Manifest
    # ========================================================

    save_manifest(
        records
    )

    successful_papers = sum(

        1

        for item
        in records

        if item.get(
            "summary",
            {},
        ).get(
            "paper_success"
        )
    )

    successful_si = sum(

        (
            item.get(
                "summary",
                {},
            ).get(
                "si_successful"
            )
            or 0
        )

        for item
        in records
    )

    skipped = sum(

        1

        for item
        in records

        if item.get(
            "skipped"
        )
    )

    failed = sum(

        1

        for item
        in records

        if (
            not item.get(
                "skipped"
            )
            and
            not item.get(
                "summary",
                {},
            ).get(
                "paper_success"
            )
        )
    )

    print()
    print()
    print(
        "=" * 80
    )

    print(
        "BATCH FINISHED"
    )

    print(
        "=" * 80
    )

    print(
        "任务 DOI 总数:",
        len(
            records
        ),
    )

    print(
        "重复跳过:",
        skipped,
    )

    print(
        "正文成功/已存在:",
        successful_papers,
    )

    print(
        "正文失败:",
        failed,
    )

    print(
        "SI成功文件数:",
        successful_si,
    )

    print()
    print(
        "Download root:"
    )

    print(
        DOWNLOAD_ROOT
    )

    print()
    print(
        "Manifest:"
    )

    print(
        MANIFEST_PATH
    )

    print(
        "=" * 80
    )


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )