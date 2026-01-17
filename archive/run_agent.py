#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pure Playwright + Claude Code CLI orchestrator (with HTML download).

Flow:
  1) Playwright navigates (optionally search)
  2) Save page "state" (screenshot, state.json, links.json, ax.json, visible_text.txt)
  3) Ask Claude (claude -p --output-format json) to decide next action
  4) Execute action (open_url / click / extract / download_html / finish)
  5) Repeat until finish or max steps

Requirements:
  - Python 3.10+
  - pip install playwright
  - playwright install chromium
  - Claude Code CLI available as `claude` in PATH
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse

from playwright.async_api import async_playwright, Page


# ---------------------------
# Config / Schema
# ---------------------------

ACTION_TYPES = {"open_url", "click", "extract", "download_html", "search", "finish"}

# What we accept for click:
# - role+name (preferred)
# - css selector
# - link_index (0-based, from DOM a tags)
CLICK_ROLE_ALLOWLIST = {
    "link",
    "button",
    "menuitem",
    "tab",
    "checkbox",
    "radio",
    "textbox",
    "combobox",
}


@dataclass
class AgentConfig:
    goal: str
    query: Optional[str]
    start_url: Optional[str]
    out_dir: Path
    headless: bool
    max_steps: int
    wait_ms: int
    allowed_domains: List[str]
    prompt_path: Path
    claude_cmd: str
    search_engine: str
    max_links: int
    max_text_chars: int
    max_html_bytes: int


# ---------------------------
# Utilities
# ---------------------------

def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def domain_allowed(url: str, allowed: List[str]) -> bool:
    if not allowed:
        return True
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in allowed)


def is_http_url(url: str) -> bool:
    try:
        u = urlparse(url)
        return u.scheme in ("http", "https") and bool(u.netloc)
    except Exception:
        return False


def clamp_text(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + "\n...[TRUNCATED]...\n"


def safe_filename(name: str) -> str:
    """
    Prevent directory traversal and normalize. Force .html extension.
    """
    name = (name or "").strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        name = "page.html"
    if not name.lower().endswith(".html"):
        name += ".html"
    return name


# ---------------------------
# Page State Capture
# ---------------------------

async def extract_links(page: Page, max_links: int) -> List[Dict[str, str]]:
    """
    Extract visible-ish links (best-effort).
    """
    js = r"""
    (maxLinks) => {
      const anchors = Array.from(document.querySelectorAll("a"))
        .map(a => {
          const href = a.href || "";
          const text = (a.innerText || a.textContent || "").trim().replace(/\s+/g, " ");
          return { href, text };
        })
        .filter(x => x.href && x.text);
      const seen = new Set();
      const uniq = [];
      for (const x of anchors) {
        const k = x.href + "||" + x.text;
        if (seen.has(k)) continue;
        seen.add(k);
        uniq.push(x);
        if (uniq.length >= maxLinks) break;
      }
      return uniq;
    }
    """
    try:
        links = await page.evaluate(js, max_links)
        if isinstance(links, list):
            return [
                {"href": str(x.get("href", "")), "text": str(x.get("text", ""))}
                for x in links
            ]
    except Exception:
        pass
    return []


async def extract_visible_text(page: Page, max_chars: int) -> str:
    js = r"""() => (document.body && document.body.innerText) ? document.body.innerText : "" """
    try:
        text = await page.evaluate(js)
        if not isinstance(text, str):
            text = str(text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return clamp_text(text.strip(), max_chars)
    except Exception:
        return ""


async def extract_accessibility_snapshot(page: Page) -> Optional[Dict[str, Any]]:
    """
    page.accessibility.snapshot() is best-effort.
    """
    try:
        snap = await page.accessibility.snapshot()
        if isinstance(snap, dict):
            return snap
    except Exception:
        return None
    return None


async def save_state(step_dir: Path, page: Page, max_links: int, max_text_chars: int) -> Dict[str, Any]:
    safe_mkdir(step_dir)

    screenshot_path = step_dir / "page.png"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception as ex:
        eprint(f"[warn] screenshot failed: {ex}")

    links = await extract_links(page, max_links=max_links)
    (step_dir / "links.json").write_text(json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")

    visible_text = await extract_visible_text(page, max_chars=max_text_chars)
    (step_dir / "visible_text.txt").write_text(visible_text, encoding="utf-8")

    ax = await extract_accessibility_snapshot(page)
    if ax is not None:
        (step_dir / "ax.json").write_text(json.dumps(ax, ensure_ascii=False, indent=2), encoding="utf-8")

    state = {
        "captured_at": now_iso(),
        "url": page.url,
        "title": await page.title(),
        "links_count": len(links),
        "links_preview": links[: min(10, len(links))],
        "visible_text_preview": visible_text[: min(2000, len(visible_text))],
        "artifacts": {
            "screenshot": "page.png",
            "links": "links.json",
            "visible_text": "visible_text.txt",
            "accessibility": "ax.json" if (step_dir / "ax.json").exists() else None,
        },
    }
    (step_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


# ---------------------------
# Claude Decision Call
# ---------------------------

def load_prompt_template(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(template: str, replacements: Dict[str, str]) -> str:
    out = template
    for k, v in replacements.items():
        out = out.replace("{{" + k + "}}", v)
    return out

def _strip_code_fences(s: str) -> str:
    """
    Remove ```json ... ``` or ``` ... ``` fences if present.
    """
    s = s.strip()
    if s.startswith("```"):
        # remove first fence line
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # remove last fence line if present
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def _extract_first_json_object(s: str) -> Dict[str, Any]:
    """
    Extract and parse the first JSON object from a string.
    Handles cases where extra text surrounds the JSON.
    """
    s = _strip_code_fences(s)

    # Fast path: whole string is JSON
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # Fallback: find first {...} by brace matching
    start = s.find("{")
    if start == -1:
        raise ValueError("No '{' found in Claude result text")

    depth = 0
    end = None
    for i in range(start, len(s)):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise ValueError("Unbalanced braces while extracting JSON object")

    chunk = s[start:end]
    obj = json.loads(chunk)
    if not isinstance(obj, dict):
        raise ValueError("Extracted JSON is not an object")
    return obj


def extract_action_from_claude_output(claude_out: Dict[str, Any]) -> Dict[str, Any]:
    """
    Claude CLI --output-format json often returns a wrapper like:
      { "type":"result", ..., "result": "```json { ... } ```" }
    or sometimes:
      { ..., "result": { ... } }

    This function returns the inner action object:
      { "action": "...", ... }
    """
    if not isinstance(claude_out, dict):
        raise ValueError("Claude output is not a dict")

    # Case 1: already an action object
    if isinstance(claude_out.get("action"), str):
        return claude_out

    # Case 2: common wrapper key "result"
    if "result" in claude_out:
        r = claude_out["result"]
        if isinstance(r, dict) and isinstance(r.get("action"), str):
            return r
        if isinstance(r, str):
            inner = _extract_first_json_object(r)
            if isinstance(inner.get("action"), str):
                return inner
            raise ValueError(f"Parsed inner JSON but no 'action' field: {inner}")

    # Case 3: other wrappers (rare; keep just in case)
    for k in ("output", "data", "action_obj"):
        r = claude_out.get(k)
        if isinstance(r, dict) and isinstance(r.get("action"), str):
            return r
        if isinstance(r, str):
            inner = _extract_first_json_object(r)
            if isinstance(inner.get("action"), str):
                return inner

    raise ValueError("Could not extract action object from Claude output")

def call_claude_json(claude_cmd: str, prompt: str) -> Dict[str, Any]:
    cmd = [claude_cmd, "-p", prompt, "--output-format", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "Claude CLI failed.\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}\n"
        )
    try:
        data = json.loads(proc.stdout)
    except Exception as ex:
        raise RuntimeError(f"Failed to parse Claude JSON output: {ex}\nRaw:\n{proc.stdout}") from ex
    return data


def validate_action(action: Dict[str, Any], allowed_domains: List[str]) -> Tuple[bool, str]:
    if not isinstance(action, dict):
        return False, "action is not a JSON object"
    t = action.get("action")
    if t not in ACTION_TYPES:
        return False, f"invalid action type: {t}"
    if "reason" not in action or not isinstance(action["reason"], str) or not action["reason"].strip():
        return False, "missing non-empty reason"

    if t == "open_url":
        url = action.get("url")
        if not isinstance(url, str) or not is_http_url(url):
            return False, "open_url requires valid http(s) url"
        if not domain_allowed(url, allowed_domains):
            return False, f"url domain not allowed: {url}"

    if t == "click":
        role = action.get("role")
        name = action.get("name")
        selector = action.get("selector")
        link_index = action.get("link_index")

        has_role = isinstance(role, str) and role in CLICK_ROLE_ALLOWLIST and isinstance(name, str) and name.strip()
        has_selector = isinstance(selector, str) and selector.strip()
        has_index = isinstance(link_index, int) and link_index >= 0

        if not (has_role or has_selector or has_index):
            return False, "click requires (role+name) or selector or link_index"

    if t == "extract":
        if "extract" in action and not isinstance(action["extract"], dict):
            return False, "extract field must be an object if present"

    if t == "download_html":
        url = action.get("url")
        if not isinstance(url, str) or not is_http_url(url):
            return False, "download_html requires valid http(s) url"
        if not domain_allowed(url, allowed_domains):
            return False, f"url domain not allowed: {url}"
        output = action.get("output")
        if not isinstance(output, str) or not output.strip():
            return False, "download_html requires non-empty output filename"
        mode = action.get("mode", "dom")
        if mode not in ("dom", "response"):
            return False, "download_html mode must be 'dom' or 'response'"

    if t == "search":
        query = action.get("query")
        if not isinstance(query, str) or not query.strip():
            return False, "search requires non-empty query"

    if t == "finish":
        pass

    return True, "ok"


# ---------------------------
# Action Execution
# ---------------------------

async def do_open_url(page: Page, url: str, wait_ms: int) -> None:
    await page.goto(url, wait_until="domcontentloaded")
    if wait_ms > 0:
        await page.wait_for_timeout(wait_ms)


async def do_click(page: Page, action: Dict[str, Any], wait_ms: int) -> None:
    role = action.get("role")
    name = action.get("name")
    selector = action.get("selector")
    link_index = action.get("link_index")

    if isinstance(role, str) and isinstance(name, str) and role in CLICK_ROLE_ALLOWLIST and name.strip():
        locator = page.get_by_role(role, name=name)
        await locator.first.click()
    elif isinstance(selector, str) and selector.strip():
        await page.locator(selector).first.click()
    elif isinstance(link_index, int) and link_index >= 0:
        js = r"""
        (idx) => {
          const as = Array.from(document.querySelectorAll("a"));
          if (idx < 0 || idx >= as.length) return false;
          as[idx].scrollIntoView({behavior: "instant", block: "center", inline: "center"});
          as[idx].click();
          return true;
        }
        """
        ok = await page.evaluate(js, link_index)
        if not ok:
            raise RuntimeError(f"link_index out of range or click failed: {link_index}")
    else:
        raise RuntimeError("Invalid click action (no role+name/selector/link_index)")

    if wait_ms > 0:
        await page.wait_for_timeout(wait_ms)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass


async def do_extract(page: Page, action: Dict[str, Any], step_dir: Path, max_text_chars: int) -> None:
    spec = action.get("extract") if isinstance(action.get("extract"), dict) else {}
    selectors = spec.get("selectors") if isinstance(spec.get("selectors"), list) else []
    mode = spec.get("mode") if isinstance(spec.get("mode"), str) else "auto"

    extracted: Dict[str, Any] = {
        "captured_at": now_iso(),
        "url": page.url,
        "title": await page.title(),
        "mode": mode,
        "selectors": selectors,
        "items": [],
    }

    if selectors:
        for sel in selectors:
            if not isinstance(sel, str) or not sel.strip():
                continue
            try:
                txt = await page.locator(sel).first.inner_text()
            except Exception:
                txt = ""
            extracted["items"].append({"selector": sel, "text": clamp_text((txt or "").strip(), max_text_chars)})
    else:
        extracted["items"].append({"selector": "BODY_INNER_TEXT", "text": await extract_visible_text(page, max_text_chars)})

    (step_dir / "extracted.json").write_text(json.dumps(extracted, ensure_ascii=False, indent=2), encoding="utf-8")
    all_text = "\n\n".join([f"[{i.get('selector')}]\n{i.get('text','')}" for i in extracted["items"]])
    (step_dir / "extracted.txt").write_text(all_text, encoding="utf-8")


async def do_download_html(page: Page, action: Dict[str, Any], step_dir: Path, wait_ms: int, max_html_bytes: int) -> None:
    """
    Save HTML to step_dir/<output>.html
    mode:
      - dom: navigate and save rendered DOM (page.content())
      - response: fetch raw HTML via context.request.get()
    """
    url = action["url"]
    mode = action.get("mode", "dom")
    out_name = safe_filename(action["output"])
    out_path = step_dir / out_name

    if mode == "dom":
        await page.goto(url, wait_until="domcontentloaded")
        if wait_ms > 0:
            await page.wait_for_timeout(wait_ms)
        html = await page.content()
    else:
        resp = await page.context.request.get(url)
        ct = (resp.headers.get("content-type") or "").lower()
        if ("text/html" not in ct) and ("application/xhtml" not in ct):
            raise RuntimeError(f"download_html(response) expected HTML but got content-type={ct}")
        html = await resp.text()

    b = html.encode("utf-8", errors="ignore")
    if len(b) > max_html_bytes:
        raise RuntimeError(f"HTML too large (>{max_html_bytes} bytes). Refuse to save.")

    out_path.write_bytes(b)
    (step_dir / "download_html.json").write_text(
        json.dumps(
            {
                "saved_at": now_iso(),
                "url": url,
                "mode": mode,
                "file": out_name,
                "bytes": len(b),
                "path": str(out_path.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------
# Search URL
# ---------------------------

def build_search_url(engine: str, query: str) -> str:
    q = quote_plus(query)
    e = engine.lower()
    if e in ("ddg", "duckduckgo"):
        return f"https://duckduckgo.com/html/?q={q}"
    if e in ("google",):
        return f"https://www.google.com/search?q={q}"
    if e in ("bing",):
        return f"https://www.bing.com/search?q={q}"
    return f"https://duckduckgo.com/html/?q={q}"


# ---------------------------
# Main Loop
# ---------------------------

async def run_loop(cfg: AgentConfig) -> None:
    safe_mkdir(cfg.out_dir)
    template = load_prompt_template(cfg.prompt_path)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=cfg.headless)
        context = await browser.new_context()
        page = await context.new_page()

        # Initial navigation
        if cfg.start_url:
            if not is_http_url(cfg.start_url):
                raise ValueError(f"Invalid --start-url: {cfg.start_url}")
            if not domain_allowed(cfg.start_url, cfg.allowed_domains):
                raise ValueError(f"Start URL not allowed by --allowed-domains: {cfg.start_url}")
            await do_open_url(page, cfg.start_url, cfg.wait_ms)
        elif cfg.query:
            search_url = build_search_url(cfg.search_engine, cfg.query)
            if not domain_allowed(search_url, cfg.allowed_domains):
                raise ValueError(
                    f"Search engine URL is not allowed by --allowed-domains.\n"
                    f"search_url={search_url}\n"
                    f"allowed={cfg.allowed_domains}\n"
                    f"Either add the search domain or omit --allowed-domains."
                )
            await do_open_url(page, search_url, cfg.wait_ms)

        for step in range(cfg.max_steps):
            step_dir = cfg.out_dir / f"step_{step:02d}"
            state = await save_state(step_dir, page, cfg.max_links, cfg.max_text_chars)

            prompt = render_prompt(
                template,
                {
                    "GOAL": cfg.goal,
                    "ALLOWED_DOMAINS": ", ".join(cfg.allowed_domains) if cfg.allowed_domains else "(no restriction)",
                    "STEP": str(step),
                    "STATE_JSON": json.dumps(state, ensure_ascii=False, indent=2),
                    "HINT_LINKS_JSON_PATH": str((step_dir / "links.json").resolve()),
                    "HINT_SCREENSHOT_PATH": str((step_dir / "page.png").resolve()),
                    "HINT_VISIBLE_TEXT_PATH": str((step_dir / "visible_text.txt").resolve()),
                    "SEARCH_ENGINE": cfg.search_engine,
                },
            )

            claude_out = call_claude_json(cfg.claude_cmd, prompt)

            try:
                action_obj = extract_action_from_claude_output(claude_out)
            except Exception as ex:
                raise RuntimeError(
                    "Unexpected Claude output JSON structure:\n"
                    + json.dumps(claude_out, ensure_ascii=False, indent=2)
                    + f"\n\nParser error: {ex}"
                ) from ex

            (step_dir / "claude_action.json").write_text(
                json.dumps(action_obj, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            ok, msg = validate_action(action_obj, cfg.allowed_domains)
            if not ok:
                raise RuntimeError(f"Claude returned invalid action: {msg}\n{json.dumps(action_obj, ensure_ascii=False, indent=2)}")

            action_type = action_obj["action"]
            eprint(f"[step {step}] action={action_type} reason={action_obj.get('reason','')[:120]}")

            if action_type == "finish":
                (cfg.out_dir / "FINISHED.txt").write_text(
                    f"Finished at {now_iso()}\nReason: {action_obj.get('reason','')}\n"
                    f"Result: {action_obj.get('result','')}\n",
                    encoding="utf-8",
                )
                break

            if action_type == "open_url":
                await do_open_url(page, action_obj["url"], cfg.wait_ms)

            elif action_type == "click":
                await do_click(page, action_obj, cfg.wait_ms)

            elif action_type == "extract":
                await do_extract(page, action_obj, step_dir, cfg.max_text_chars)

            elif action_type == "download_html":
                await do_download_html(page, action_obj, step_dir, cfg.wait_ms, cfg.max_html_bytes)

            elif action_type == "search":
                query = action_obj["query"]
                search_url = build_search_url(cfg.search_engine, query)
                if not domain_allowed(search_url, cfg.allowed_domains):
                    raise RuntimeError(
                        f"Search engine URL is not allowed by --allowed-domains.\n"
                        f"search_url={search_url}\n"
                        f"allowed={cfg.allowed_domains}\n"
                        f"Either add the search domain or omit --allowed-domains."
                    )
                await do_open_url(page, search_url, cfg.wait_ms)

            else:
                raise RuntimeError(f"Unhandled action type: {action_type}")

        await context.close()
        await browser.close()


def parse_args() -> AgentConfig:
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal", required=True, help="Goal / task statement for the agent.")
    ap.add_argument("--query", help="Search query (will open a search engine page). If omitted, agent can decide.")
    ap.add_argument("--start-url", help="Start URL (skip search).")
    ap.add_argument("--out-dir", default="out", help="Output directory for states/artifacts.")
    ap.add_argument("--headless", action="store_true", help="Run browser headless.")
    ap.add_argument("--max-steps", type=int, default=12, help="Max decision steps.")
    ap.add_argument("--wait-ms", type=int, default=700, help="Wait after actions (ms).")
    ap.add_argument("--allowed-domains", default="", help="Comma-separated allowlist of domains. Empty=allow all.")
    ap.add_argument("--prompt", default="prompts/navigator.md", help="Navigator prompt template path.")
    ap.add_argument("--claude-cmd", default=os.environ.get("CLAUDE_CMD", "claude"), help="Claude CLI command.")
    ap.add_argument("--search-engine", default="duckduckgo", help="duckduckgo|bing|google (default: duckduckgo)")
    ap.add_argument("--max-links", type=int, default=25, help="Max links to extract each step.")
    ap.add_argument("--max-text-chars", type=int, default=20000, help="Max chars of visible text to save.")
    ap.add_argument("--max-html-bytes", type=int, default=5 * 1024 * 1024, help="Max HTML bytes to save (default 5MB).")

    ns = ap.parse_args()

    allowed = [d.strip().lower() for d in ns.allowed_domains.split(",") if d.strip()]
    return AgentConfig(
        goal=ns.goal,
        query=ns.query,
        start_url=ns.start_url,
        out_dir=Path(ns.out_dir),
        headless=bool(ns.headless),
        max_steps=int(ns.max_steps),
        wait_ms=int(ns.wait_ms),
        allowed_domains=allowed,
        prompt_path=Path(ns.prompt),
        claude_cmd=str(ns.claude_cmd),
        search_engine=str(ns.search_engine),
        max_links=int(ns.max_links),
        max_text_chars=int(ns.max_text_chars),
        max_html_bytes=int(ns.max_html_bytes),
    )


def main() -> None:
    cfg = parse_args()
    asyncio.run(run_loop(cfg))


if __name__ == "__main__":
    main()
