# crawler.py
from __future__ import annotations

from dataclasses import dataclass, asdict
from urllib.parse import urlparse, urljoin, urldefrag
from collections import deque
from typing import Any

from playwright.async_api import async_playwright

from pathlib import Path
import re


# ----------------------------
# Screenshots (Option A: only on problems)
# ----------------------------
SCREEN_DIR = Path("runs/screens")
SCREEN_DIR.mkdir(parents=True, exist_ok=True)


def safe_filename(url: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", url)[:120].strip("_")
    return s or "page"


# ----------------------------
# Dead button detection (functional autonomy)
# ----------------------------
DANGEROUS_TEXT = ("delete", "remove", "pay", "checkout", "logout", "unsubscribe", "cancel order")


async def detect_dead_clicks(page, limit: int = 5) -> list[dict]:
    """
    Click a few visible clickables and flag those that cause no observable change.
    Observable change = URL change OR significant DOM length change OR some network activity.
    Returns list of {selector, text, reason, url}
    """
    results: list[dict] = []

    # Collect candidates in page context (fast + avoids heavy selectors)
    try:
        candidates = await page.evaluate(
            """
            () => {
              const els = Array.from(document.querySelectorAll(
                'button, a[href], [role="button"], input[type="submit"], input[type="button"]'
              ));

              const visible = els.filter(el => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden') return false;
                if (r.width < 12 || r.height < 12) return false;
                if (el.disabled) return false;
                return true;
              });

              function selectorFor(el){
                let sel = el.tagName.toLowerCase();
                if (el.id) return sel + "#" + el.id;

                const cls = (el.className && typeof el.className === "string")
                  ? el.className.trim().split(/\\s+/).slice(0,2).join(".")
                  : "";
                if (cls) sel += "." + cls;
                return sel;
              }

              return visible.slice(0, 25).map(el => ({
                sel: selectorFor(el),
                text: (el.innerText || el.value || "").trim().slice(0, 80),
                tag: el.tagName.toLowerCase()
              }));
            }
            """
        )
    except Exception:
        return results

    # Click a few
    for item in (candidates or [])[:limit]:
        sel = (item or {}).get("sel") or ""
        txt = (item or {}).get("text") or ""
        if not sel:
            continue

        # safety: skip obviously dangerous actions
        if txt and any(k in txt.lower() for k in DANGEROUS_TEXT):
            continue

        req_counter = {"n": 0}

        def on_req(_req):
            req_counter["n"] += 1

        try:
            before_url = canonicalize(page.url)
            before_dom_len = await page.evaluate("() => document.documentElement.outerHTML.length")

            # Track *any* request after click
            page.on("request", on_req)

            # Try click
            await page.click(sel, timeout=2500)

            # Allow handlers / navigation / fetch
            await page.wait_for_timeout(900)

            after_url = page.url
            after_dom_len = await page.evaluate("() => document.documentElement.outerHTML.length")
            net_n = req_counter["n"]

            # Detach handler
            try:
                page.off("request", on_req)
            except Exception:
                pass

            url_changed = (after_url != before_url)
            dom_changed = (abs(after_dom_len - before_dom_len) > 250)
            net_changed = (net_n > 0)

            if not (url_changed or dom_changed or net_changed):
                results.append(
                    {
                        "selector": sel,
                        "text": txt,
                        "reason": "no_change",
                        "url": before_url,
                    }
                )

        except Exception:
            # ignore click failures (overlays, detached nodes, etc.)
            try:
                page.off("request", on_req)
            except Exception:
                pass
            continue

    return results


# ----------------------------
# Filters
# ----------------------------
BAD_PATHS = {
    "/head", "/body", "/html", "/div", "/span",
    "/p", "/a", "/form", "/button", "/script",
}

# ignore Vite dev endpoints & other dev noise
BAD_SUBSTRINGS = ("@vite", "__vite", "vite", "hmr", "hot-update", "sockjs")

ASSET_EXTS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".mjs", ".map",
    ".jsx", ".tsx", ".ts",
    ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".zip",
)

BAD_PATH_PREFIXES = (
    "/src/",
    "/assets/",
    "/static/",
    "/node_modules/",
    "/favicon",
)


def is_bad_path(url: str) -> bool:
    try:
        path = (urlparse(url).path or "").lower()
        return any(path.startswith(pfx) for pfx in BAD_PATH_PREFIXES)
    except Exception:
        return False


def is_asset_url(url: str) -> bool:
    u = (url or "").lower()
    return any(u.endswith(ext) for ext in ASSET_EXTS)


def is_bad_href(href: str) -> bool:
    if not href:
        return True
    href = href.strip()

    # ignore junk links
    if href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return True

    # ignore html tag-like paths
    if href in BAD_PATHS:
        return True

    return False


def is_dev_noise(url: str) -> bool:
    s = (url or "").lower()
    return any(sub in s for sub in BAD_SUBSTRINGS)


def same_domain(base_url: str, candidate_url: str) -> bool:
    """
    Same-site check that treats localhost and 127.0.0.1 as equivalent (for local dev).
    """
    try:
        a = urlparse(base_url)
        b = urlparse(candidate_url)

        def norm_host(h: str | None) -> str:
            if h in ("127.0.0.1", "0.0.0.0"):
                return "localhost"
            return h or ""

        a_host = norm_host(a.hostname)
        b_host = norm_host(b.hostname)

        a_port = a.port or (443 if a.scheme == "https" else 80)
        b_port = b.port or (443 if b.scheme == "https" else 80)

        return a_host == b_host and a_port == b_port
    except Exception:
        return False

def canonicalize(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    u, _ = urldefrag(u)

    p = urlparse(u)
    scheme = p.scheme.lower() or "http"
    host = (p.hostname or "").lower()
    if host in ("127.0.0.1", "0.0.0.0"):
        host = "localhost"
    port = p.port or (443 if scheme == "https" else 80)

    path = p.path or "/"

    # normalize root
    if path == "":
        path = "/"

    # remove trailing slash except root
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    # IMPORTANT: ignore query for BFS to avoid duplicates like ?tab=1
    # (you can revisit later with “duplicate URL” hygiene)
    query = ""

    netloc = host
    if not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    return f"{scheme}://{netloc}{path}{('?' + query) if query else ''}"

def normalize_url(base_url: str, href: str) -> str | None:
    if not href:
        return None
    href = href.strip()

    if href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None

    abs_url = urljoin(base_url, href)
    abs_url, _ = urldefrag(abs_url)

    if not abs_url.startswith(("http://", "https://")):
        return None

    if is_asset_url(abs_url):
        return None

    if is_dev_noise(abs_url):
        return None

    if is_bad_path(abs_url):
        return None

    return abs_url


# ----------------------------
# Link check helpers (reduce false positives)
# ----------------------------
def is_probably_spa_route(u: str) -> bool:
    """
    Heuristic: same-domain links like /orders, /menu, /product/p1 are SPA routes.
    Fetching them with raw GET can return 404 depending on server config,
    even though clicking works (client-side router).
    """
    try:
        path = (urlparse(u).path or "").lower()
        # If it has a file extension, it's likely a real file, not a route
        if re.search(r"\.[a-z0-9]{1,6}$", path):
            return False
        # route-like paths
        return path not in ("", "/")
    except Exception:
        return False


def should_check_link(u: str, base_url: str) -> bool:
    """
    Only check links that are meaningful to validate via HTTP request,
    avoiding SPA internal route false positives.
    """
    if not u:
        return False
    s = u.strip()
    s_low = s.lower()

    if s_low.startswith(("javascript:", "mailto:", "tel:")):
        return False

    # skip fragment-only links and links with fragments (often in-page navigation)
    if "#" in s_low:
        return False

    if not s_low.startswith(("http://", "https://")):
        return False

    if is_asset_url(s_low) or is_dev_noise(s_low) or is_bad_path(s_low):
        return False

    # Same-domain route-like links are often SPA routes → skip HTTP check
    if same_domain(base_url, s) and is_probably_spa_route(s):
        return False

    return True


@dataclass
class PageResult:
    url: str
    title: str
    dom: str
    telemetry: dict
    links: list[str]
    logs: dict

    def to_dict(self) -> dict:
        return asdict(self)


class WebCrawler:
    async def crawl_site(self, base_url: str, max_pages: int = 20, progress_cb=None):
        """
        Returns:
          - pages: list[dict]  (each dict has url/title/dom/telemetry/links/logs)
        """

        def push(pct: int, msg: str):
            if progress_cb:
                progress_cb(pct, msg)

        push(5, f"Starting browser... (max_pages={max_pages})")

        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        visited: set[str] = set()
        base_url = canonicalize(base_url)
        q = deque([base_url])
        pages: list[dict] = []

        try:
            while q and len(pages) < max_pages:
                url = q.popleft()
                url = canonicalize(url)
                if not url:
                    continue
                if url in visited:
                    continue
                visited.add(url)

                pct = int(10 + (len(pages) / max_pages) * 60)
                push(pct, f"Crawling {url}")

                page = await context.new_page()

                # ---- logs expected by Analyzer (compact)
                console_errors: list[dict[str, Any]] = []
                page_errors: list[str] = []
                request_failures: list[dict[str, Any]] = []

                # ---- telemetry extras
                api_failures: list[dict[str, Any]] = []
                unauthorized_api: list[dict[str, Any]] = []
                link_checks: list[dict[str, Any]] = []
                dead_clicks: list[dict[str, Any]] = []

                # ---- event handlers
                def on_console(msg):
                    try:
                        if msg.type in ("error", "warning"):
                            console_errors.append({"type": msg.type, "text": msg.text})
                    except Exception:
                        pass

                def on_pageerror(exc):
                    try:
                        page_errors.append(str(exc))
                    except Exception:
                        pass

                def on_requestfailed(req):
                    try:
                        failure = req.failure
                        if isinstance(failure, dict):
                            error_text = failure.get("errorText", "")
                        elif isinstance(failure, str):
                            error_text = failure
                        else:
                            error_text = ""
                        request_failures.append({"url": req.url, "failure": error_text})
                    except Exception:
                        pass

                def on_response(resp):
                    try:
                        req = resp.request
                        rt = (req.resource_type or "").lower()
                        if rt in ("xhr", "fetch", "websocket"):
                            if resp.status >= 400:
                                api_failures.append(
                                    {"url": resp.url, "status": resp.status, "resource_type": rt}
                                )
                            if resp.status in (401, 403):
                                unauthorized_api.append({"url": resp.url, "status": resp.status})
                    except Exception:
                        pass

                page.on("console", on_console)
                page.on("pageerror", on_pageerror)
                page.on("requestfailed", on_requestfailed)
                page.on("response", on_response)

                shot_path = ""  # default: no screenshot

                try:
                    await page.goto(url, timeout=30000, wait_until="domcontentloaded")

                    # SPA settle
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass

                    # scroll for lazy content
                    for _ in range(2):
                        await page.mouse.wheel(0, 2000)
                        await page.wait_for_timeout(300)

                    dom = await page.content()
                    title = await page.title()
                    current_url = canonicalize(page.url)
                    # If navigation ended up at a different canonical URL (redirect/trailing slash),
                    # mark that as visited too so we don't crawl it again.
                    if current_url and current_url not in visited:
                        visited.add(current_url)
                    # perf
                    try:
                        perf = await page.evaluate("() => performance.timing")
                    except Exception:
                        perf = {}

                    # NEW: dead button detection (safe subset)
                    try:
                        dead_clicks = await detect_dead_clicks(page, limit=5)
                    except Exception:
                        dead_clicks = []

                    # --- extract href + form[action]
                    # (Do NOT collect [src] for crawling; it causes dev/build file URLs)
                    hrefs = await page.evaluate(
                        """
                        () => {
                            const urls = new Set();
                            document.querySelectorAll('[href]').forEach(el => {
                                const v = el.getAttribute('href');
                                if (v) urls.add(v);
                            });
                            document.querySelectorAll('form[action]').forEach(el => {
                                const v = el.getAttribute('action');
                                if (v) urls.add(v);
                            });
                            return Array.from(urls);
                        }
                        """
                    )

                    out_links_set: set[str] = set()
                    for href in hrefs:
                        if is_bad_href(href):
                            continue
                        norm = normalize_url(current_url, href)
                        if norm:
                            norm = canonicalize(norm)
                        if not norm:
                            continue
                        if same_domain(base_url, norm):
                            if norm not in out_links_set:
                                out_links_set.add(norm)
                            if norm not in visited and norm not in q:
                                q.append(norm)

                    out_links = sorted(out_links_set)

                    # ---- link checks (limit)
                    anchor_urls = await page.evaluate(
                        """
                        () => Array.from(document.querySelectorAll('a[href]'))
                              .map(a => a.href)
                              .filter(h => !!h)
                              .slice(0, 120)
                        """
                    )

                    checked = 0
                    for u in anchor_urls:
                        if checked >= 60:
                            break
                        if not should_check_link(u, base_url):
                            continue

                        checked += 1
                        try:
                            resp = await context.request.get(u, timeout=8000, max_redirects=8)
                            status = resp.status

                            if status in (401, 403, 451):
                                link_checks.append({"url": u, "status": status, "kind": "blocked"})
                            elif status >= 400:
                                link_checks.append({"url": u, "status": status, "kind": "broken"})
                        except Exception as e:
                            link_checks.append(
                                {"url": u, "status": 0, "kind": "failed", "error": str(e)[:120]}
                            )

                    logs = {
                        "console": console_errors[:50],
                        "page_errors": page_errors[:50],
                        "request_failures": request_failures[:50],
                    }

                    # ---- Option A screenshot rule: only on problems
                    has_problems = (
                        len(console_errors) > 0
                        or len(page_errors) > 0
                        or len(request_failures) > 0
                        or len(api_failures) > 0
                        or len(dead_clicks) > 0
                        or any(x.get("kind") in ("broken", "failed") for x in link_checks)
                    )

                    if has_problems:
                        sshot_path = str(SCREEN_DIR / f"{safe_filename(current_url)}.png")
                        try:
                            await page.screenshot(path=shot_path, full_page=True)
                        except Exception:
                            shot_path = ""

                    telemetry = {
                        "url": current_url,
                        "title": title,
                        "screenshot_path": shot_path,
                        "performance": perf,
                        "dead_clicks": dead_clicks,  # ✅ NEW
                        "api_failures": api_failures[:80],
                        "unauthorized_api": unauthorized_api[:80],
                        "link_checks": link_checks[:120],
                        "logs": logs,
                    }

                    pages.append(
                        PageResult(
                            url=current_url,
                            title=title,
                            dom=dom,
                            telemetry=telemetry,
                            links=out_links,
                            logs=logs,
                        ).to_dict()
                    )

                except Exception as e:
                    logs = {
                        "console": console_errors[:50],
                        "page_errors": page_errors[:50],
                        "request_failures": request_failures[:50],
                    }

                    # For navigation failures, screenshot is valuable
                    shot_path = str(SCREEN_DIR / f"{safe_filename(url)}.png")
                    try:
                        await page.screenshot(path=shot_path, full_page=True)
                    except Exception:
                        shot_path = ""

                    pages.append(
                        PageResult(
                            url=url,
                            title="",
                            dom="",
                            telemetry={
                                "url": url,
                                "error": str(e),
                                "screenshot_path": shot_path,
                                "performance": {},
                                "dead_clicks": [],  # ✅ NEW
                                "api_failures": api_failures[:80],
                                "unauthorized_api": unauthorized_api[:80],
                                "link_checks": link_checks[:120],
                                "logs": logs,
                            },
                            links=[],
                            logs=logs,
                        ).to_dict()
                    )

                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

            push(80, "Crawl complete. Returning results...")
            return pages

        finally:
            push(95, "Closing browser...")
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
            try:
                await p.stop()
            except Exception:
                pass