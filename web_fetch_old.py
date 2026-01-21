#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, csv, json, time, hashlib, mimetypes, subprocess, tempfile, argparse, sys, glob
from pathlib import Path
from urllib.parse import urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
import chardet
from bs4 import BeautifulSoup

# Optional but recommended: fast main-content extractor
try:
    import trafilatura
    HAS_TRA = True
except Exception:
    HAS_TRA = False

# PDF text extractors
from pdfminer.high_level import extract_text as pdf_extract_text
try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except Exception:
    HAS_FITZ = False

# デフォルトの出力ディレクトリ（後でmainで年に基づいて更新）
OUT_DIR = Path("out_2020")
PDF_DIR = Path("out_pdf_2020")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OrdinanceTextBot/1.0; +https://example.invalid)"
}

# Custom SSL adapter for compatibility with problematic servers
class SSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.set_ciphers('DEFAULT@SECLEVEL=1')
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)

def create_session():
    """Create a requests session with custom SSL adapter."""
    s = requests.Session()
    s.mount('https://', SSLAdapter())
    s.headers.update(HEADERS)
    return s

def sha256_of_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def fetch(url: str, timeout=30):
    """Fetch with HEAD fallback to GET if HEAD not allowed."""
    s = create_session()
    try:
        r = s.head(url, allow_redirects=True, timeout=timeout)
        if r.status_code >= 400 or ("text/html" in r.headers.get("content-type","").lower() and int(r.headers.get("content-length","0") or 0) == 0):
            raise Exception("HEAD not usable, try GET")
        return r, None
    except Exception:
        r = s.get(url, allow_redirects=True, timeout=timeout)
        return r, r.content  # when GET was used, return content too

def guess_filename(url: str) -> str:
    name = os.path.basename(urlparse(url).path) or "index"
    # Remove query parameters if present
    name = name.split("?")[0]
    return name

def safe_filename(text: str) -> str:
    """Convert text to safe filename by removing/replacing problematic characters."""
    # Replace spaces and problematic characters with underscore
    text = re.sub(r'[^\w\-.]', '_', text)
    # Remove consecutive underscores
    text = re.sub(r'_+', '_', text)
    return text.strip('_')

def normalize_text(txt: str) -> str:
    # heuristic cleanups for Japanese legal text
    txt = txt.replace("\u3000", " ")            # full-width space -> half
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()

def cleanup_extracted_text(text: str, url: str = "") -> str:
    """Remove common noise blocks like TOC and internal IDs from ordinance pages.
    Heuristics target g-reiki style pages but are safe generically.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    # Drop trailing sections starting at known markers
    markers = {"条項目次", "体系情報", "沿革情報"}
    cut_idx = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s in markers:
            cut_idx = i
            break
    if cut_idx is not None:
        lines = lines[:cut_idx]

    # Remove internal ID-like lines e.g., e000000123
    id_re = re.compile(r"^[A-Za-z]\d{6,}$")
    lines = [ln for ln in lines if not id_re.match(ln.strip())]

    # Merge common split lines caused by inline markup/newlines
    def merge_splits(ls):
        out = []
        i = 0
        changed = False
        # Patterns
        art_header_re = re.compile(r"^\s*第[〇一二三四五六七八九十百千万0-9]+条\s*$")
        enum_paren_re = re.compile(r"^\s*[（(][0-9０-９一二三四五六七八九十]+[)）]\s*$")
        enum_numsolo_re = re.compile(r"^\s*[0-9０-９]+\s*$")
        enum_kata_solo_re = re.compile(r"^\s*[アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲンイロハニホヘトチリヌルヲ]\s*$")
        enum_kata_end_re = re.compile(r"[アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン]$")
        cross_ref_head_re = re.compile(
            r"^(?:同条|次条|本条|条例|規則)?第[0-9０-９〇一二三四五六七八九十百千万]+(条|項|号)"
            r"|^(?:前条|前項|同条|同項|各条|各項)\b"
        )
        token_solo_re = re.compile(r"^\s*(前項|同項|前条|同条)\s*$")
        paren_block_re = re.compile(r"^\s*[（(].*[)）]\s*$")
        lone_open_re = re.compile(r"^\s*[（(]\s*$")
        lone_close_re = re.compile(r"^\s*[)）]\s*$")
        cjk_solo_re = re.compile(r"^\s*[\u4E00-\u9FFF]\s*$")
        eos_punct = set("。．.？！!？」』)）]")
        while i < len(ls):
            cur = ls[i]
            nxt = ls[i+1] if i+1 < len(ls) else None
            prv = out[-1] if out else None
            # Join article header line with next
            if nxt and art_header_re.match(cur) and nxt.strip():
                out.append(cur.strip() + " " + nxt.lstrip())
                i += 2
                changed = True
                continue
            # Join standalone enumerator like (1) with next
            if nxt and enum_paren_re.match(cur) and nxt.strip():
                out.append(cur.strip() + " " + nxt.lstrip())
                i += 2
                changed = True
                continue
            # Join standalone paragraph number like "2" with next
            if nxt and enum_numsolo_re.match(cur) and nxt.strip():
                out.append(cur.strip() + " " + nxt.lstrip())
                i += 2
                changed = True
                continue
            # Join standalone katakana enumerator like "ア" with next
            if nxt and enum_kata_solo_re.match(cur) and nxt.strip():
                out.append(cur.strip() + " " + nxt.lstrip())
                i += 2
                changed = True
                continue
            # Join cross-reference breaks: prev … 条例\n第11条 / … 同条\n第1項 など
            if nxt and cross_ref_head_re.match(nxt.strip()):
                if cur and (cur[-1] not in eos_punct):
                    out.append(cur.rstrip() + nxt.lstrip())
                    i += 2
                    changed = True
                    continue
            # Join if previous ends with 条/項/号 and next begins with a particle or punctuation
            if nxt:
                prev_end = cur.rstrip()[-1:] if cur else ""
                next_start = nxt.lstrip()[:1]
                if prev_end in {"条","項","号"} and next_start in {"の","に","を","へ","と","や","及","並","、",",","(","（"}:
                    out.append(cur.rstrip() + nxt.lstrip())
                    i += 2
                    changed = True
                    continue
            # Join when current line is just a token like 前項/同条
            if nxt and token_solo_re.match(cur):
                out.append(cur.strip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            # Join parentheses blocks on their own line with previous
            if paren_block_re.match(cur) and prv:
                out[-1] = prv.rstrip() + cur.strip()
                i += 1
                changed = True
                continue
            # If line ends with an opening parenthesis, join with next
            if nxt and (cur.rstrip().endswith("（") or cur.rstrip().endswith("(")):
                out.append(cur.rstrip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            # If next is a standalone katakana enumerator and current ends with 'から', join
            if nxt and enum_kata_solo_re.match(nxt) and cur.rstrip().endswith("から"):
                out.append(cur.rstrip() + nxt.strip())
                i += 2
                changed = True
                continue
            # If current ends with katakana enumerator and next starts with 'まで', join
            if nxt and enum_kata_end_re.search(cur.rstrip()) and nxt.lstrip().startswith("まで"):
                out.append(cur.rstrip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            # If current ends with katakana enumerator and next starts with 'から', join
            if nxt and enum_kata_end_re.search(cur.rstrip()) and nxt.lstrip().startswith("から"):
                out.append(cur.rstrip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            # If current ends with '次の' and next starts with katakana enumerator, join
            if nxt and cur.rstrip().endswith("次の") and (enum_kata_solo_re.match(nxt) or enum_kata_end_re.match(nxt.lstrip()[-1:]) or re.match(r"^\s*[アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲ]", nxt)):
                out.append(cur.rstrip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            # If current ends with '、', join with next unless next is a new header/enumerator
            if nxt and cur.rstrip().endswith("、"):
                next_stripped = nxt.strip()
                if not (art_header_re.match(next_stripped) or enum_paren_re.match(next_stripped) or enum_numsolo_re.match(next_stripped) or enum_kata_solo_re.match(next_stripped)):
                    out.append(cur.rstrip() + next_stripped)
                    i += 2
                    changed = True
                    continue
            # If current ends with CJK and next starts with CJK and not sentence end, join
            if nxt and cur and (cur[-1] not in eos_punct) and re.match(r"[\u4E00-\u9FFF]$", cur[-1]) and re.match(r"^[\u4E00-\u9FFF]", nxt.lstrip()):
                out.append(cur.rstrip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            # If next line is a lone closing paren, join
            if nxt and lone_close_re.match(nxt):
                out.append(cur.rstrip() + nxt.strip())
                i += 2
                changed = True
                continue
            # If current is a lone opening paren, join with next
            if nxt and lone_open_re.match(cur):
                out.append(cur.strip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            # If current ends with closing paren and next starts with 第 (e.g., law article), join
            if nxt and (cur.rstrip().endswith("）") or cur.rstrip().endswith(")")) and nxt.lstrip().startswith("第"):
                out.append(cur.rstrip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            # If current ends with '、' and next starts with cross-ref head like 前/同/次/第/条例/規則, join
            if nxt and cur.rstrip().endswith("、") and nxt.lstrip()[:1] in {"前","同","次","第","条","規","本"}:
                out.append(cur.rstrip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            out.append(cur)
            i += 1
        return out, changed

    # Iteratively apply merge until stable
    changed = True
    while changed:
        lines, changed = merge_splits(lines)

    # Remove duplicate consecutive empty lines and trim
    cleaned = []
    prev_empty = False
    for ln in lines:
        is_empty = (ln.strip() == "")
        if is_empty and prev_empty:
            continue
        cleaned.append(ln)
        prev_empty = is_empty
    text = "\n".join(cleaned).strip()
    # Inline tidy-up: collapse spaces around parentheses and after 条/項/号, and between 第..条 and particles
    subs = [
        (r"（\s+", "（"),
        (r"\s+）", "）"),
        (r"\(\s+", "("),
        (r"\s+\)", ")"),
        (r"(条|項|号)\s+([のにをへとや及び並び])", r"\1\2"),
        (r"第\s*([0-9０-９〇一二三四五六七八九十百千万]+)\s*条\s+([のにをへとや及び並び])", r"第\1条\2"),
    ]
    for pat, rep in subs:
        text = re.sub(pat, rep, text)
    return text

def extract_html_text(html_bytes: bytes, url: str) -> str:
    # encoding detection
    enc = chardet.detect(html_bytes).get("encoding") or "utf-8"
    html = html_bytes.decode(enc, errors="replace")

    if HAS_TRA:
        try:
            main = trafilatura.extract(html, url=url, include_comments=False, include_links=False)
            if main and len(main) > 200:
                return normalize_text(main)
        except Exception:
            pass

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script","style","noscript","header","footer","nav"]):
        tag.decompose()
    text = soup.get_text("\n")
    # collapse menu-like very short lines heuristic
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    text = "\n".join(lines)
    return normalize_text(text)

def extract_html_text_bs_only(html_bytes: bytes) -> str:
    """BeautifulSoup-only extractor (bypass Trafilatura)."""
    try:
        enc = chardet.detect(html_bytes).get("encoding") or "utf-8"
    except Exception:
        enc = "utf-8"
    html = html_bytes.decode(enc, errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script","style","noscript","header","footer","nav"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    text = "\n".join(lines)
    return normalize_text(text)

def pdf_text_fast(path: Path) -> str:
    """Try pdfminer first; if too short, try PyMuPDF blocks with sort."""
    try:
        t = pdf_extract_text(str(path)) or ""
    except Exception:
        t = ""
    if len(t.strip()) >= 200:
        return normalize_text(t)
    # try PyMuPDF if available
    if HAS_FITZ:
        try:
            doc = fitz.open(path)
            blocks = []
            for page in doc:
                blocks.append(page.get_text("text", sort=True))
            t2 = "\n".join(blocks)
            return normalize_text(t2)
        except Exception:
            pass
    return normalize_text(t)

def is_scanned_pdf(path: Path, char_threshold=200) -> bool:
    """Heuristic: extract text and count; scanned PDFs usually yield near zero."""
    txt = pdf_text_fast(path)
    return len(txt) < char_threshold

def ocrmypdf_available() -> bool:
    try:
        subprocess.run(["ocrmypdf","--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False

def run_ocrmypdf(in_pdf: Path, out_pdf: Path, lang="jpn+eng"):
    cmd = [
        "ocrmypdf",
        "--skip-text",          # do not OCR pages with embedded text
        "--optimize", "3",      # max optimization
        "--language", lang,
        "--output-type","pdf",
        "--jobs", "2",
        str(in_pdf),
        str(out_pdf),
    ]
    subprocess.run(cmd, check=True)

def process_url(url: str, meta: dict, html_completed: set):
    # Use metadata for better filename
    if "municipality" in meta and "doc_type" in meta:
        municipality_safe = safe_filename(meta["municipality"])
        doc_type_safe = safe_filename(meta["doc_type"])
        fname_base = f"{municipality_safe}_{doc_type_safe}"
    else:
        fname_base = safe_filename(guess_filename(url))
    
    # Determine if this is a PDF URL
    r_test, _ = fetch(url)
    ct = r_test.headers.get("content-type","").split(";")[0].lower()
    is_pdf = "pdf" in ct or url.lower().endswith(".pdf")
    
    if is_pdf:
        # Check if HTML versions exist for both Ordinance and Regulation
        municipality = meta.get("municipality", "")
        if municipality:
            municipality_safe = safe_filename(municipality)
            ordinance_html = OUT_DIR / f"{municipality_safe}_Ordinance_HTML.txt"
            regulation_html = OUT_DIR / f"{municipality_safe}_Regulation_HTML.txt"
            
            # Skip PDF if both HTML versions exist
            if ordinance_html.exists() and regulation_html.exists():
                print(f"    [SKIP] HTML versions exist for {municipality}, skipping PDF")
                return {
                    "url": url,
                    "municipality": municipality,
                    "prefecture": meta.get("prefecture", ""),
                    "doc_type": meta.get("doc_type", ""),
                    "status": "skipped",
                    "method": "html_exists",
                    "output_pdf": None
                }
        
        # Check if PDF already downloaded
        out_pdf = PDF_DIR / f"{fname_base}.pdf"
        if out_pdf.exists():
            print(f"    [SKIP] Already exists: {out_pdf}")
            return {
                "url": url,
                "municipality": meta.get("municipality", ""),
                "prefecture": meta.get("prefecture", ""),
                "doc_type": meta.get("doc_type", ""),
                "output_pdf": str(out_pdf),
                "status": "skipped",
                "method": "already_exists"
            }
        
        # Download PDF
        r, body = fetch(url)
        if body is None:
            r_get = create_session().get(url, timeout=30)
            body = r_get.content
        
        pdf_hash = sha256_of_bytes(body)
        out_pdf.write_bytes(body)
        
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return {
            "url": url,
            "content_type": ct,
            "fetched_at": now,
            "method": "pdf_download",
            "bytes_sha256": pdf_hash,
            "output_pdf": str(out_pdf),
        }
    
    # HTML processing
    out_txt = OUT_DIR / f"{fname_base}.txt"
    overwrite = os.getenv("OVERWRITE", "0") == "1"
    if out_txt.exists() and not overwrite:
        print(f"    [SKIP] Already exists: {out_txt}")
        return {
            "url": url,
            "municipality": meta.get("municipality", ""),
            "prefecture": meta.get("prefecture", ""),
            "doc_type": meta.get("doc_type", ""),
            "output_txt": str(out_txt),
            "status": "skipped",
            "method": "already_exists"
        }
    
    r, body = fetch(url)
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    
    record = {
        "url": url,
        "content_type": ct,
        "fetched_at": now,
        "method": None,
        "bytes_sha256": None,
        "output_txt": None,
    }

    if body is None:
        r_get = create_session().get(url, timeout=30)
        body = r_get.content
    rec_hash = sha256_of_bytes(body)
    record["bytes_sha256"] = rec_hash
    # Choose extraction path
    netloc = urlparse(url).netloc.lower()
    force_bs = os.getenv("FORCE_BS", "0") == "1"
    force_tra = os.getenv("FORCE_TRA", "0") == "1"
    use_bs = force_bs or (("g-reiki.net" in netloc) and not force_tra)
    if use_bs:
        text = extract_html_text_bs_only(body)
        method_used = "beautifulsoup"
    else:
        text = extract_html_text(body, url)
        method_used = "trafilatura" if HAS_TRA else "beautifulsoup"
    # Common cleanup
    text = cleanup_extracted_text(text, url)
    out_txt.write_text(text, encoding="utf-8")
    record["method"] = method_used
    record["output_txt"] = str(out_txt)
    return record

def get_available_years():
    """利用可能な年別URLファイルのリストを取得"""
    pattern = "urls_*.csv"
    files = glob.glob(pattern)
    years = []
    for f in files:
        match = re.search(r'urls_(\d{4})\.csv', f)
        if match:
            years.append(match.group(1))
    return sorted(years)

def parse_year_range(year_input):
    """年の範囲文字列を解析して年のリストを返す"""
    if not year_input:
        return []
    
    if '-' in year_input:
        # 範囲指定（例: 2014-2018）
        try:
            start_year, end_year = year_input.split('-', 1)
            start_year = int(start_year.strip())
            end_year = int(end_year.strip())
            if start_year > end_year:
                print(f"エラー: 開始年（{start_year}）が終了年（{end_year}）より大きいです。")
                sys.exit(1)
            return [str(year) for year in range(start_year, end_year + 1)]
        except ValueError:
            print(f"エラー: 年の範囲指定が無効です: {year_input}")
            print("正しい形式: YYYY-YYYY (例: 2014-2018)")
            sys.exit(1)
    else:
        # 単年指定（例: 2015）
        try:
            year = int(year_input.strip())
            return [str(year)]
        except ValueError:
            print(f"エラー: 年の指定が無効です: {year_input}")
            print("正しい形式: YYYY または YYYY-YYYY (例: 2015 または 2014-2018)")
            sys.exit(1)

def get_urls_file_paths(years=None):
    """年のリストに基づいてURLファイルのパスリストを返す"""
    if not years:
        # デフォルトのurls.csvを使用
        if os.path.exists("urls.csv"):
            return ["urls.csv"]
        else:
            # urls.csvが存在しない場合、最新の年別ファイルを使用
            available_years = get_available_years()
            if available_years:
                latest_year = available_years[-1]
                print(f"urls.csv が見つかりません。最新の年別ファイル urls_{latest_year}.csv を使用します。")
                return [f"urls_{latest_year}.csv"]
            else:
                print("エラー: URLファイルが見つかりません。")
                sys.exit(1)
    
    # 指定された年のファイルパスを生成
    file_paths = []
    available_years = get_available_years()
    missing_years = []
    
    for year in years:
        urls_path = f"urls_{year}.csv"
        if os.path.exists(urls_path):
            file_paths.append(urls_path)
        else:
            missing_years.append(year)
    
    if missing_years:
        print(f"エラー: 以下の年のファイルが見つかりません: {', '.join(missing_years)}")
        if available_years:
            print(f"利用可能な年: {', '.join(available_years)}")
        else:
            print("年別URLファイルが見つかりません。")
        sys.exit(1)
    
    return file_paths

def process_single_file(urls_path):
    """単一のURLファイルを処理"""
    global OUT_DIR, PDF_DIR
    
    # ファイル名から年を抽出して出力ディレクトリを設定
    match = re.search(r'urls_(\d{4})\.csv', urls_path)
    if match:
        detected_year = match.group(1)
        OUT_DIR = Path(f"out_{detected_year}")
        PDF_DIR = Path(f"out_pdf_{detected_year}")
    else:
        # デフォルトのディレクトリを使用
        OUT_DIR = Path("out_2020")
        PDF_DIR = Path("out_pdf_2020")

    # Optional suffix to keep multiple runs separate (e.g., bs vs traf)
    suffix = os.getenv("OUT_DIR_TAG", "").strip()
    if suffix:
        OUT_DIR = Path(f"{OUT_DIR}_{suffix}")
        PDF_DIR = Path(f"{PDF_DIR}_{suffix}")
    
    print(f"\n{'='*60}")
    print(f"Processing: {urls_path}")
    print(f"Output directory: {OUT_DIR}")
    print(f"PDF directory: {PDF_DIR}")
    print(f"{'='*60}")
    
    index_path = OUT_DIR / "index.jsonl"
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(PDF_DIR, exist_ok=True)
    
    # read URLs from CSV with all URL columns
    url_entries = []
    with open(urls_path, newline="", encoding="utf-8") as f:
        # Skip empty lines at the beginning
        lines = f.readlines()
        # Find the first non-empty line as header
        start_idx = 0
        for i, line in enumerate(lines):
            if line.strip():
                start_idx = i
                break
        
        # Create a new reader from the valid lines
        from io import StringIO
        valid_csv = "".join(lines[start_idx:])
        reader = csv.DictReader(StringIO(valid_csv))
        
        print(f"CSV Headers: {reader.fieldnames}")
        for row in reader:
            municipality = row.get("Municipality", "").strip()
            prefecture = row.get("Prefecture", "").strip()
            
            # Extract URLs from all URL columns (HTML and PDF)
            url_columns = [
                "Ordinance_HTML",
                "Regulation_HTML",
                "Ordinance_PDF",
                "Regulation_PDF"
            ]
            
            for col_name in url_columns:
                url = row.get(col_name) or ""
                url = url.strip().strip('"')
                
                if url and url.startswith("http"):
                    url_entries.append({
                        "url": url,
                        "municipality": municipality,
                        "prefecture": prefecture,
                        "doc_type": col_name
                    })

    print(f"Total URLs found: {len(url_entries)}")
    
    # Build set of municipalities that have HTML completed
    html_completed = set()
    
    meta = {}
    results = []
    total = len(url_entries)
    success_count = 0
    skip_count = 0
    error_count = 0
    
    for idx, entry in enumerate(url_entries, 1):
        url = entry["url"]
        try:
            # Pass metadata to process_url
            meta_info = {
                "municipality": entry["municipality"],
                "prefecture": entry["prefecture"],
                "doc_type": entry["doc_type"]
            }
            rec = process_url(url, meta_info, html_completed)
            # Add metadata to record
            rec["municipality"] = entry["municipality"]
            rec["prefecture"] = entry["prefecture"]
            rec["doc_type"] = entry["doc_type"]
            results.append(rec)
            
            if rec.get("status") == "skipped":
                skip_count += 1
                skip_reason = rec.get("method", "already_exists")
                if skip_reason == "html_exists":
                    print(f"[{idx}/{total}] [SKIP] {entry['municipality']} ({entry['doc_type']}) -> HTML versions exist")
                else:
                    print(f"[{idx}/{total}] [SKIP] {entry['municipality']} ({entry['doc_type']}) -> Already exists")
            else:
                success_count += 1
                output_file = rec.get('output_pdf') or rec.get('output_txt', 'N/A')
                print(f"[{idx}/{total}] [OK] {entry['municipality']} ({entry['doc_type']}) -> {output_file} ({rec['method']})")
        except Exception as e:
            error_count += 1
            print(f"[{idx}/{total}] [ERR] {entry['municipality']} ({entry['doc_type']}) {url} -> {e}")
            results.append({
                "url": url,
                "municipality": entry["municipality"],
                "prefecture": entry["prefecture"],
                "doc_type": entry["doc_type"],
                "error": str(e)
            })

    with open(index_path, "w", encoding="utf-8") as w:
        for rec in results:
            w.write(json.dumps(rec, ensure_ascii=False) + "\n")
    
    print(f"\nCompleted processing {urls_path}")
    print(f"  Success: {success_count}")
    print(f"  Skipped: {skip_count}")
    print(f"  Errors:  {error_count}")
    print(f"  Results saved to: {index_path}")
    
    return success_count, skip_count, error_count, len(results)



def main(urls_path=None, year_input=None):
    """メイン処理関数"""
    print(f"Starting web_fetch.py...")
    
    if urls_path:
        # 手動でファイルが指定された場合
        file_paths = [urls_path]
    else:
        # 年の範囲または単年から処理対象ファイルを決定
        years = parse_year_range(year_input)
        file_paths = get_urls_file_paths(years)
    
    print(f"Processing {len(file_paths)} file(s)")
    
    total_success = 0
    total_skip = 0
    total_error = 0
    total_urls = 0
    
    for file_path in file_paths:
        success, skip, error, urls = process_single_file(file_path)
        total_success += success
        total_skip += skip
        total_error += error
        total_urls += urls
    
    # 最終的な統計情報を表示
    print(f"\n{'='*60}")
    print(f"全体の処理結果:")
    print(f"  処理ファイル数: {len(file_paths)}")
    print(f"  総URL数: {total_urls}")
    print(f"  成功: {total_success}")
    print(f"  スキップ: {total_skip}")
    print(f"  エラー: {total_error}")
    print(f"{'='*60}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="自治体条例・規則のWebスクレイピングツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python web_fetch.py                    # デフォルト（urls.csv または最新年）
  python web_fetch.py --year 2014        # urls_2014.csv を使用
  python web_fetch.py -y 2015            # urls_2015.csv を使用
  python web_fetch.py --year 2014-2018   # 2014年から2018年まで順次処理
  python web_fetch.py -y 2016-2017       # 2016年と2017年を処理
  python web_fetch.py --list-years       # 利用可能な年のリストを表示
  python web_fetch.py --urls-file custom.csv  # カスタムファイルを指定
        """
    )
    parser.add_argument("--year", "-y", type=str, help="処理対象の年（例: 2014, 2015, 2014-2018）")
    parser.add_argument("--list-years", "-l", action="store_true", help="利用可能な年のリストを表示")
    parser.add_argument("--urls-file", type=str, help="URLファイルのパス（手動指定）")
    
    args = parser.parse_args()
    
    if args.list_years:
        available_years = get_available_years()
        if available_years:
            print("利用可能な年:")
            for year in available_years:
                print(f"  {year} (urls_{year}.csv)")
        else:
            print("年別URLファイルが見つかりません。")
        sys.exit(0)
    
    main(urls_path=args.urls_file, year_input=args.year)
