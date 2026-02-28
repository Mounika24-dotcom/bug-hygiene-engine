# main.py
from __future__ import annotations

import json
import shutil
import hashlib
from pathlib import Path
from datetime import datetime
from collections import Counter, deque
from urllib.parse import urlparse, urldefrag

from crawler import WebCrawler
from analyzer import Analyzer
from classifier import PageTypeClassifier
from scorer import score_from_scorer_py

print(">>> RUNNING main.py FROM:", __file__)

# ----------------------------
# DEDUPE
# ----------------------------
def dedupe_issues(issues: list[dict]) -> list[dict]:
    """
    Remove duplicate defects so the same issue doesn't repeat.

    Notes:
    - For Broken/Blocked links: unique by (issue, hint/url)
    - For everything else: unique by (issue, severity, tag, locator, hint)
    """
    seen = set()
    out = []

    for it in issues or []:
        issue = (it.get("issue") or "").strip()
        severity = (it.get("severity") or "").strip()
        tag = (it.get("tag") or "").strip()
        locator = (it.get("locator") or "").strip()
        hint = (it.get("hint") or "").strip()

        if issue in ("Broken Link", "External Link Blocked", "Link Check Failed"):
            key = (issue, hint)
        else:
            key = (issue, severity, tag, locator, hint)

        if key in seen:
            continue
        seen.add(key)
        out.append(it)

    return out


# ----------------------------
# RUNS / ARTIFACT SAVING
# ----------------------------
RUNS_DIR = Path("runs")


def make_run_dir() -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RUNS_DIR / f"run_{stamp}"
    (run_dir / "screens").mkdir(parents=True, exist_ok=True)
    return run_dir


def safe_json_dump(obj, path: Path):
    """
    Atomic-ish write: write to .tmp then replace, so you don't get 0B JSON files.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def flatten_issues(page_results: list[dict]) -> list[dict]:
    """
    Convert nested per-page issues into a flat list for easy filtering/export.
    """
    out = []
    for pr in page_results or []:
        for it in pr.get("issues", []) or []:
            out.append({
                "page_url": pr.get("url", ""),
                "page_title": pr.get("title", ""),
                "page_type": pr.get("page_type", ""),
                "issue": it.get("issue", ""),
                "severity": it.get("severity", ""),
                "tag": it.get("tag", ""),
                "locator": it.get("locator", ""),
                "hint": it.get("hint", ""),
                "suggestion": it.get("suggestion", ""),
                "evidence": it.get("evidence", ""),
                "issue_id": it.get("issue_id", ""),
                "screenshot_path": pr.get("screenshot_path", ""),
            })
    return out


def copy_screenshot_into_run(shot_path: str, run_screens_dir: Path) -> str:
    """
    Copies screenshot into run folder and returns the new path (as string).
    If missing or copy fails, returns original path.
    """
    if not shot_path:
        return shot_path
    try:
        src = Path(shot_path)
        if not src.exists():
            return shot_path
        dst = run_screens_dir / src.name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        return str(dst)
    except Exception:
        return shot_path


# ----------------------------
# NAV GRAPH (Option 1: JSON nodes/edges)
# ----------------------------
def canonicalize_for_graph(u: str) -> str:
    """
    Keep it consistent with crawler canonicalize behavior:
    - remove fragments
    - normalize host for localhost
    - drop query (optional)
    - remove trailing slash (except root)
    """
    u = (u or "").strip()
    if not u:
        return ""
    u, _ = urldefrag(u)

    p = urlparse(u)
    scheme = (p.scheme or "http").lower()
    host = (p.hostname or "").lower()
    if host in ("127.0.0.1", "0.0.0.0"):
        host = "localhost"
    port = p.port or (443 if scheme == "https" else 80)

    path = p.path or "/"
    if path == "":
        path = "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    # ignore query in graph (matches crawler)
    netloc = host
    if not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    return f"{scheme}://{netloc}{path}"


def build_site_graph(pages: list[dict], base_url: str) -> dict:
    """
    Build a navigation graph from crawler output:
    pages[i] has:
      - url (string)
      - links (list of string)
    Output:
      {
        "nodes": [{"id": "...", "type": "page", "url": "..."}],
        "edges": [{"source": "...", "target": "...", "type": "LINKS_TO"}],
        "metrics": {"orphans": [...], "depths": {...}, "max_depth": int, "cycles": int, "duplicates": {...}}
      }
    """
    base = canonicalize_for_graph(base_url)

    # collect nodes
    urls = []
    for p in pages or []:
        u = canonicalize_for_graph(p.get("url", ""))
        if u:
            urls.append(u)
    url_set = set(urls)

    # edges + indegree
    edges = []
    indeg = Counter()
    adj = {}

    for p in pages or []:
        src = canonicalize_for_graph(p.get("url", ""))
        if not src:
            continue
        adj.setdefault(src, set())

        for raw_tgt in (p.get("links") or []):
            tgt = canonicalize_for_graph(raw_tgt)
            if not tgt:
                continue
            # Only include targets we actually crawled (cleaner graph)
            if tgt not in url_set:
                continue

            if tgt not in adj[src]:
                adj[src].add(tgt)
                edges.append({"source": src, "target": tgt, "type": "LINKS_TO"})
                indeg[tgt] += 1

    # Orphans (in-degree 0 except base/home)
    orphans = sorted([u for u in url_set if indeg.get(u, 0) == 0 and u != base])

    # Depths (BFS from base)
    depths = {}
    if base in url_set:
        q = deque([(base, 0)])
        depths[base] = 0
        while q:
            node, d = q.popleft()
            for nxt in adj.get(node, []):
                if nxt not in depths:
                    depths[nxt] = d + 1
                    q.append((nxt, d + 1))
    else:
        # if base not crawled, seed with any page as depth 0
        if urls:
            depths[urls[0]] = 0

    max_depth = max(depths.values()) if depths else 0
    deep_pages = sorted([u for u, d in depths.items() if d > 4])

    # Simple cycle count (DFS)
    cycles = 0
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {u: WHITE for u in url_set}

    def dfs(u: str):
        nonlocal cycles
        color[u] = GRAY
        for v in adj.get(u, []):
            if color[v] == WHITE:
                dfs(v)
            elif color[v] == GRAY:
                cycles += 1
        color[u] = BLACK

    for u in url_set:
        if color[u] == WHITE:
            dfs(u)

    nodes = [{"id": u, "type": "page", "url": u} for u in sorted(url_set)]

    return {
        "nodes": nodes,
        "edges": edges,
        "metrics": {
            "base": base,
            "orphans": orphans,
            "depths": depths,          # dict url -> depth
            "max_depth": max_depth,
            "deep_pages": deep_pages,
            "cycles": cycles,
            "nodes": len(nodes),
            "edges": len(edges),
        },
    }


# ----------------------------
# HYGIENE SCORE
# ----------------------------
class Scorer:
    WEIGHTS = {"Critical": 15, "Medium": 8, "Low": 3}

    def score_page(self, issues: list[dict]) -> tuple[int, dict]:
        counts = {"Critical": 0, "Medium": 0, "Low": 0}
        for it in issues or []:
            sev = (it.get("severity") or "Low").strip()
            if sev in counts:
                counts[sev] += 1

        penalty = sum(counts[s] * self.WEIGHTS[s] for s in counts)
        score = max(0, 100 - penalty)

        breakdown = {
            "counts": counts,
            "weights": self.WEIGHTS,
            "penalty": penalty,
            "formula": "score = max(0, 100 - (Critical*15 + Medium*8 + Low*3))",
        }
        return score, breakdown


def make_issue_id(issue: dict) -> str:
    """
    Stable ID for an issue. Helps DKG + dedupe tracking.
    """
    parts = [
        (issue.get("issue") or "").strip(),
        (issue.get("severity") or "").strip(),
        (issue.get("tag") or "").strip(),
        (issue.get("locator") or "").strip(),
        (issue.get("hint") or "").strip(),
    ]
    raw = "||".join(parts).encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:12]


# ----------------------------
# ENGINE
# ----------------------------
async def run_engine(url: str, max_pages: int = 20, progress_cb=None):
    def push(p: int, msg: str):
        if progress_cb:
            progress_cb(p, msg)

    crawler = WebCrawler()
    analyzer = Analyzer()
    page_type_classifier = PageTypeClassifier()
    scorer = Scorer()

    # ✅ SAFE DEFAULTS (prevents "score not associated" crash)
    page_results: list[dict] = []
    site_scores: list[int] = []
    issues_flat: list[dict] = []
    site_score_avg: int = 100
    site_breakdown: dict = {}
    site_issues: list[dict] = []

    # Create per-run folder
    run_dir = make_run_dir()
    run_screens = run_dir / "screens"

    push(10, f"Run folder created: {run_dir.name}")

    # Crawl
    push(20, "Crawling pages...")
    pages = await crawler.crawl_site(
        url,
        max_pages=max_pages,
        progress_cb=progress_cb,
    )

    # Save raw crawl output early
    safe_json_dump(pages, run_dir / "pages.json")

    # Build navigation graph (structure & flow)
    site_graph = build_site_graph(pages, base_url=url)
    safe_json_dump(site_graph, run_dir / "site_graph.json")

    # Analyze
    total = max(1, len(pages))

    for i, p in enumerate(pages, start=1):
        page_url = p.get("url", "")
        push(20 + int(70 * (i / total)), f"Analyzing page {i}/{total}: {page_url}")

        try:
            dom = p.get("dom", "") or ""
            tele = p.get("telemetry", {}) or {}

            url_ = page_url or tele.get("url", "")
            title = tele.get("title", "") or p.get("title", "")

            issues = analyzer.detect_issues(dom, telemetry=tele) or []
            issues = dedupe_issues(issues)

            # Attach stable issue IDs
            for it in issues:
                it["issue_id"] = make_issue_id(it)

            page_type = page_type_classifier.classify_page_type(
                dom=dom,
                url=url_,
                title=title,
            )

        except Exception as e:
            issues = [{
                "issue": "Analyzer Crash",
                "severity": "Critical",
                "tag": "engine",
                "locator": "page",
                "hint": str(e)[:160],
                "suggestion": "Fix Analyzer.detect_issues telemetry parsing.",
                "evidence": "",
            }]
            for it in issues:
                it["issue_id"] = make_issue_id(it)

            url_ = page_url or ""
            title = (p.get("telemetry", {}) or {}).get("title") or p.get("title", "")
            page_type = "unknown"

        # Screenshot path from crawler telemetry
        shot = (p.get("telemetry", {}) or {}).get("screenshot_path", "")
        shot = copy_screenshot_into_run(shot, run_screens)

        # Hygiene score (per page)
        page_score, score_breakdown = scorer.score_page(issues)
        site_scores.append(page_score)

        page_results.append({
            "url": url_,
            "title": title,
            "page_type": page_type,
            "issues_count": len(issues),
            "issues": issues,
            "hygiene_score": page_score,
            "score_breakdown": score_breakdown,
            "screenshot_path": shot,
        })

    # Save analyzed artifacts
    safe_json_dump(page_results, run_dir / "results.json")

    issues_flat = flatten_issues(page_results)
    safe_json_dump(issues_flat, run_dir / "issues.json")

    # ✅ Site score (average of per-page scores)
    # -------------------------
# Site Score (USE YOUR SCORER FUNCTION)
# -------------------------
    site_score_avg, site_breakdown = score_from_scorer_py(issues_flat)

    summary = {
        "base_url": url,
        "pages_crawled": len(pages),
        "pages_analyzed": len(page_results),
        "total_issues": len(issues_flat),
        "site_hygiene_score_avg": site_score_avg,
        "run_dir": str(run_dir),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "nav_metrics": site_graph.get("metrics", {}),
        "graph_files": {
            "site_graph": str(run_dir / "site_graph.json"),
            "pages": str(run_dir / "pages.json"),
            "results": str(run_dir / "results.json"),
            "issues": str(run_dir / "issues.json"),
        },
    }
    safe_json_dump(summary, run_dir / "summary.json")

    push(100, "Done ✅")

    # -------------------------
    # RETURN CLEAN SITE-LEVEL DATA
    # -------------------------
    return {
        "score": site_score_avg,        # ✅ SITE score
        "breakdown": site_breakdown,    # ✅ SITE breakdown
        "issues": issues_flat,          # ✅ ALL issues
        "page_results": page_results,
        "summary": summary,
    }