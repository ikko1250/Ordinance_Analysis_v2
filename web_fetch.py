#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time, hashlib, subprocess, argparse, sys, sqlite3
from typing import Optional
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

# デフォルトの出力ディレクトリ
OUT_DIR = Path("out_db")
PDF_DIR = Path("out_pdf_db")
DB_PATH = Path("data/ordinance_data.db")

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
    """Fetch with GET and return the response plus content."""
    s = create_session()
    r = s.get(url, allow_redirects=True, timeout=timeout)
    return r, r.content

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

# Pre-compiled regex patterns for line merging (module-level for performance)
_ART_HEADER_RE = re.compile(r"^\s*第[〇一二三四五六七八九十百千万0-9]+条\s*$")
_ENUM_PAREN_RE = re.compile(r"^\s*[（(][0-9\uFF10-\uFF19一二三四五六七八九十]+[)）]\s*$")
_ENUM_NUMSOLO_RE = re.compile(r"^\s*[0-9\uFF10-\uFF19]+\s*$")
_ENUM_KATA_SOLO_RE = re.compile(r"^\s*[アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲンイロハニホヘトチリヌルヲ]\s*$")
_ENUM_KATA_END_RE = re.compile(r"[アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン]$")
_CROSS_REF_HEAD_RE = re.compile(
    r"^(?:同条|次条|本条|条例|規則)?第[0-9\uFF10-\uFF19〇一二三四五六七八九十百千万]+(条|項|号)"
    r"|^(?:前条|前項|同条|同項|各条|各項)\b"
)
_TOKEN_SOLO_RE = re.compile(r"^\s*(前項|同項|前条|同条)\s*$")
_PAREN_BLOCK_RE = re.compile(r"^\s*[（(].*[)）]\s*$")
_LONE_OPEN_RE = re.compile(r"^\s*[（(]\s*$")
_LONE_CLOSE_RE = re.compile(r"^\s*[)）]\s*$")
_CJK_SOLO_RE = re.compile(r"^\s*[\u4E00-\u9FFF]\s*$")
_ID_RE = re.compile(r"^[A-Za-z]\d{6,}$")
_EOS_PUNCT = set("。．.？！!？」』）)")

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
    lines = [ln for ln in lines if not _ID_RE.match(ln.strip())]

    # Merge common split lines caused by inline markup/newlines
    def merge_splits(ls):
        out = []
        i = 0
        changed = False
        while i < len(ls):
            cur = ls[i]
            nxt = ls[i+1] if i+1 < len(ls) else None
            prv = out[-1] if out else None
            # Join article header line with next
            if nxt and _ART_HEADER_RE.match(cur) and nxt.strip():
                out.append(cur.strip() + " " + nxt.lstrip())
                i += 2
                changed = True
                continue
            # Join standalone enumerator like (1) with next
            if nxt and _ENUM_PAREN_RE.match(cur) and nxt.strip():
                out.append(cur.strip() + " " + nxt.lstrip())
                i += 2
                changed = True
                continue
            # Join standalone paragraph number like "2" with next
            if nxt and _ENUM_NUMSOLO_RE.match(cur) and nxt.strip():
                out.append(cur.strip() + " " + nxt.lstrip())
                i += 2
                changed = True
                continue
            # Join standalone katakana enumerator like "ア" with next
            if nxt and _ENUM_KATA_SOLO_RE.match(cur) and nxt.strip():
                out.append(cur.strip() + " " + nxt.lstrip())
                i += 2
                changed = True
                continue
            # Join cross-reference breaks: prev … 条例\n第11条 / … 同条\n第1項 など
            if nxt and _CROSS_REF_HEAD_RE.match(nxt.strip()):
                if cur and (cur[-1] not in _EOS_PUNCT):
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
            if nxt and _TOKEN_SOLO_RE.match(cur):
                out.append(cur.strip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            # Join parentheses blocks on their own line with previous
            if _PAREN_BLOCK_RE.match(cur) and prv:
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
            if nxt and _ENUM_KATA_SOLO_RE.match(nxt) and cur.rstrip().endswith("から"):
                out.append(cur.rstrip() + nxt.strip())
                i += 2
                changed = True
                continue
            # If current ends with katakana enumerator and next starts with 'まで', join
            if nxt and _ENUM_KATA_END_RE.search(cur.rstrip()) and nxt.lstrip().startswith("まで"):
                out.append(cur.rstrip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            # If current ends with katakana enumerator and next starts with 'から', join
            if nxt and _ENUM_KATA_END_RE.search(cur.rstrip()) and nxt.lstrip().startswith("から"):
                out.append(cur.rstrip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            # If current ends with '次の' and next starts with katakana enumerator, join
            if nxt and cur.rstrip().endswith("次の") and (_ENUM_KATA_SOLO_RE.match(nxt) or _ENUM_KATA_END_RE.match(nxt.lstrip()[-1:]) or re.match(r"^\s*[アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲ]", nxt)):
                out.append(cur.rstrip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            # If current ends with '、', join with next unless next is a new header/enumerator
            if nxt and cur.rstrip().endswith("、"):
                next_stripped = nxt.strip()
                if not (_ART_HEADER_RE.match(next_stripped) or _ENUM_PAREN_RE.match(next_stripped) or _ENUM_NUMSOLO_RE.match(next_stripped) or _ENUM_KATA_SOLO_RE.match(next_stripped)):
                    out.append(cur.rstrip() + next_stripped)
                    i += 2
                    changed = True
                    continue
            # If current ends with CJK and next starts with CJK and not sentence end, join
            if nxt and cur and (cur[-1] not in _EOS_PUNCT) and _CJK_SOLO_RE.match(cur[-1]) and re.match(r"^[\u4E00-\u9FFF]", nxt.lstrip()):
                out.append(cur.rstrip() + nxt.lstrip())
                i += 2
                changed = True
                continue
            # If next line is a lone closing paren, join
            if nxt and _LONE_CLOSE_RE.match(nxt):
                out.append(cur.rstrip() + nxt.strip())
                i += 2
                changed = True
                continue
            # If current is a lone opening paren, join with next
            if nxt and _LONE_OPEN_RE.match(cur):
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

def build_output_basename(meta: dict, url: str) -> str:
    impl_id = meta.get("implementation_date_id")
    municipality = meta.get("municipality", "")
    doc_kind = meta.get("doc_kind", "")
    parts = []
    if impl_id is not None:
        parts.append(str(impl_id))
    if municipality:
        parts.append(municipality)
    if doc_kind:
        parts.append(doc_kind)
    if not parts:
        parts.append(guess_filename(url))
    return safe_filename("_".join(parts))

def process_url(url: str, meta: dict):
    fname_base = build_output_basename(meta, url)
    overwrite = os.getenv("OVERWRITE", "0") == "1"
    pdf_by_ext = url.lower().endswith(".pdf")
    out_pdf = PDF_DIR / f"{fname_base}.pdf"
    out_txt = OUT_DIR / f"{fname_base}.txt"

    if pdf_by_ext and out_pdf.exists() and not overwrite:
        print(f"    [SKIP] Already exists: {out_pdf}")
        return {
            "url": url,
            "municipality": meta.get("municipality", ""),
            "prefecture": meta.get("prefecture", ""),
            "doc_kind": meta.get("doc_kind", ""),
            "output_pdf": str(out_pdf),
            "status": "skipped",
            "method": "already_exists"
        }

    r, body = fetch(url)
    if r.status_code >= 400:
        raise Exception(f"HTTP {r.status_code} fetching {url}")
    ct = r.headers.get("content-type","").split(";")[0].lower()
    is_pdf = pdf_by_ext or ("pdf" in ct)

    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    if is_pdf:
        if out_pdf.exists() and not overwrite:
            print(f"    [SKIP] Already exists: {out_pdf}")
            return {
                "url": url,
                "municipality": meta.get("municipality", ""),
                "prefecture": meta.get("prefecture", ""),
                "doc_kind": meta.get("doc_kind", ""),
                "output_pdf": str(out_pdf),
                "status": "skipped",
                "method": "already_exists"
            }

        pdf_hash = sha256_of_bytes(body)
        out_pdf.write_bytes(body)
        return {
            "url": url,
            "content_type": ct,
            "fetched_at": now,
            "method": "pdf_download",
            "bytes_sha256": pdf_hash,
            "output_pdf": str(out_pdf),
        }

    if out_txt.exists() and not overwrite:
        print(f"    [SKIP] Already exists: {out_txt}")
        return {
            "url": url,
            "municipality": meta.get("municipality", ""),
            "prefecture": meta.get("prefecture", ""),
            "doc_kind": meta.get("doc_kind", ""),
            "output_txt": str(out_txt),
            "status": "skipped",
            "method": "already_exists"
        }

    record = {
        "url": url,
        "content_type": ct,
        "fetched_at": now,
        "method": None,
        "bytes_sha256": None,
        "output_txt": None,
    }
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

def ensure_output_dirs():
    """出力ディレクトリを準備"""
    global OUT_DIR, PDF_DIR
    suffix = os.getenv("OUT_DIR_TAG", "").strip()
    if suffix:
        OUT_DIR = Path(f"{OUT_DIR}_{suffix}")
        PDF_DIR = Path(f"{PDF_DIR}_{suffix}")
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(PDF_DIR, exist_ok=True)

def init_ordinance_files_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ordinance_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            municipality_id INTEGER NOT NULL,
            ordinance_id INTEGER NOT NULL,
            implementation_date_id INTEGER,
            file_kind TEXT NOT NULL,
            path TEXT NOT NULL,
            source_url TEXT,
            bytes_sha256 TEXT,
            extract_method TEXT,
            fetched_at TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(municipality_id) REFERENCES municipalities(id),
            FOREIGN KEY(ordinance_id) REFERENCES ordinances(id),
            FOREIGN KEY(implementation_date_id) REFERENCES implementation_dates(id),
            UNIQUE(ordinance_id, file_kind, implementation_date_id)
        )
        """
    )
    conn.commit()

def select_best_implementation_date(conn: sqlite3.Connection, ordinance_id: int):
    row = conn.execute(
        """
        SELECT id, implementation_date, description
        FROM implementation_dates
        WHERE ordinance_id = ?
        ORDER BY
            CASE description
                WHEN '改正施行' THEN 0
                WHEN '初回施行' THEN 1
                ELSE 2
            END,
            implementation_date DESC,
            id DESC
        LIMIT 1
        """,
        (ordinance_id,),
    ).fetchone()
    return row

def fetch_ordinances(conn: sqlite3.Connection, limit: Optional[int]):
    query = """
        SELECT
            o.id AS ordinance_id,
            o.municipality_id,
            o.ordinance_name,
            o.url,
            o.enactment_year,
            o.promulgation_date,
            m.prefecture_name,
            m.municipality_name
        FROM ordinances o
        JOIN municipalities m ON m.id = o.municipality_id
        WHERE o.url IS NOT NULL AND TRIM(o.url) != ''
        ORDER BY o.id
    """
    params = ()
    if limit:
        query += " LIMIT ?"
        params = (limit,)
    return conn.execute(query, params).fetchall()

def record_output_file(conn: sqlite3.Connection, meta: dict, rec: dict, file_kind: str, path: str):
    conn.execute(
        """
        INSERT INTO ordinance_files (
            municipality_id,
            ordinance_id,
            implementation_date_id,
            file_kind,
            path,
            source_url,
            bytes_sha256,
            extract_method,
            fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ordinance_id, file_kind, implementation_date_id)
        DO UPDATE SET
            municipality_id=excluded.municipality_id,
            path=excluded.path,
            source_url=excluded.source_url,
            bytes_sha256=excluded.bytes_sha256,
            extract_method=excluded.extract_method,
            fetched_at=excluded.fetched_at
        """,
        (
            meta["municipality_id"],
            meta["ordinance_id"],
            meta["implementation_date_id"],
            file_kind,
            path,
            rec.get("url"),
            rec.get("bytes_sha256"),
            rec.get("method"),
            rec.get("fetched_at"),
        ),
    )

def process_db(conn: sqlite3.Connection, db_path: Path, limit: Optional[int]):
    print(f"\n{'='*60}")
    print(f"Processing DB: {db_path}")
    print(f"Output directory: {OUT_DIR}")
    print(f"PDF directory: {PDF_DIR}")
    print(f"{'='*60}")

    index_path = OUT_DIR / "index.jsonl"
    rows = fetch_ordinances(conn, limit)
    total = len(rows)
    print(f"Total URLs found: {total}")

    success_count = 0
    skip_count = 0
    error_count = 0

    with open(index_path, "w", encoding="utf-8") as w:
        for idx, row in enumerate(rows, 1):
            url = (row["url"] or "").strip()
            if not url.startswith("http"):
                error_count += 1
                rec = {
                    "url": url,
                    "municipality": row["municipality_name"],
                    "prefecture": row["prefecture_name"],
                    "doc_kind": "条例",
                    "ordinance_id": row["ordinance_id"],
                    "municipality_id": row["municipality_id"],
                    "ordinance_name": row["ordinance_name"],
                    "enactment_year": row["enactment_year"],
                    "error": "invalid_url",
                }
                w.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"[{idx}/{total}] [ERR] {row['municipality_name']} (条例) {url} -> invalid_url")
                continue

            impl = select_best_implementation_date(conn, row["ordinance_id"])
            if impl is None:
                error_count += 1
                rec = {
                    "url": url,
                    "municipality": row["municipality_name"],
                    "prefecture": row["prefecture_name"],
                    "doc_kind": "条例",
                    "ordinance_id": row["ordinance_id"],
                    "municipality_id": row["municipality_id"],
                    "ordinance_name": row["ordinance_name"],
                    "enactment_year": row["enactment_year"],
                    "error": "implementation_date_not_found",
                }
                w.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"[{idx}/{total}] [ERR] {row['municipality_name']} (条例) {url} -> implementation_date_not_found")
                continue

            meta = {
                "municipality": row["municipality_name"],
                "prefecture": row["prefecture_name"],
                "doc_kind": "条例",
                "ordinance_id": row["ordinance_id"],
                "municipality_id": row["municipality_id"],
                "implementation_date_id": impl["id"],
                "ordinance_name": row["ordinance_name"],
                "enactment_year": row["enactment_year"],
            }

            try:
                rec = process_url(url, meta)
                rec["municipality"] = row["municipality_name"]
                rec["prefecture"] = row["prefecture_name"]
                rec["doc_kind"] = "条例"
                rec["ordinance_id"] = row["ordinance_id"]
                rec["municipality_id"] = row["municipality_id"]
                rec["implementation_date_id"] = impl["id"]
                rec["implementation_date"] = impl["implementation_date"]
                rec["implementation_description"] = impl["description"]
                rec["ordinance_name"] = row["ordinance_name"]
                rec["enactment_year"] = row["enactment_year"]

                if rec.get("status") == "skipped":
                    skip_count += 1
                    output_file = rec.get("output_pdf") or rec.get("output_txt", "N/A")
                    print(f"[{idx}/{total}] [SKIP] {row['municipality_name']} (条例) -> {output_file}")
                else:
                    success_count += 1
                    output_file = rec.get("output_pdf") or rec.get("output_txt", "N/A")
                    print(f"[{idx}/{total}] [OK] {row['municipality_name']} (条例) -> {output_file} ({rec.get('method')})")

                file_kind = "pdf" if rec.get("output_pdf") else "html_text"
                out_path = rec.get("output_pdf") or rec.get("output_txt")
                if out_path:
                    record_output_file(conn, meta, rec, file_kind, out_path)
                w.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception as e:
                error_count += 1
                rec = {
                    "url": url,
                    "municipality": row["municipality_name"],
                    "prefecture": row["prefecture_name"],
                    "doc_kind": "条例",
                    "ordinance_id": row["ordinance_id"],
                    "municipality_id": row["municipality_id"],
                    "implementation_date_id": impl["id"],
                    "ordinance_name": row["ordinance_name"],
                    "enactment_year": row["enactment_year"],
                    "error": str(e),
                }
                w.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"[{idx}/{total}] [ERR] {row['municipality_name']} (条例) {url} -> {e}")

    conn.commit()
    print(f"\nCompleted processing DB")
    print(f"  Success: {success_count}")
    print(f"  Skipped: {skip_count}")
    print(f"  Errors:  {error_count}")
    print(f"  Results saved to: {index_path}")

    return success_count, skip_count, error_count, total

def main(db_path=DB_PATH, limit: Optional[int] = None):
    """メイン処理関数"""
    print(f"Starting web_fetch.py...")

    db_path = Path(db_path)
    if not db_path.exists():
        print(f"エラー: DBが見つかりません: {db_path}")
        sys.exit(1)

    ensure_output_dirs()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_ordinance_files_table(conn)
    try:
        process_db(conn, db_path, limit)
    finally:
        conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="自治体条例・規則のWebスクレイピングツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python web_fetch.py
  python web_fetch.py --db-path data/ordinance_data.db
  python web_fetch.py --limit 10
        """
    )
    parser.add_argument("--db-path", type=str, default=str(DB_PATH), help="SQLite DBのパス")
    parser.add_argument("--limit", type=int, default=None, help="テスト用に先頭N件のみ処理")
    
    args = parser.parse_args()

    main(db_path=args.db_path, limit=args.limit)
