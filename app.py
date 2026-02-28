# app.py
from __future__ import annotations
import io
import csv
import pandas as pd
import asyncio
import json
import math
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st
from main import run_engine  # async def run_engine(url, max_pages=20, progress_cb=None)

RUNS_DIR = Path("runs")
CRAWL_BUDGET = 20  # fixed; no UI


# -----------------------------
# Helpers: artifacts
# -----------------------------
def safe_read_json(path: Path, default):
    try:
        if not path.exists() or path.stat().st_size == 0:
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def find_latest_run_dir() -> Optional[Path]:
    if not RUNS_DIR.exists():
        return None
    runs = [p for p in RUNS_DIR.iterdir() if p.is_dir() and p.name.startswith("run_")]
    if not runs:
        return None
    return sorted(runs, key=lambda p: p.name)[-1]


# -----------------------------
# Helpers: async runner
# -----------------------------
def run_async(coro):
    """
    Works in normal python + in Streamlit (which may already have a loop).
    """
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

def build_report_rows(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    One row per issue (UI-deduped or full — you choose before calling).
    Columns: issue, severity, page_url, suggestion
    """
    rows = []
    for it in issues or []:
        rows.append({
            "Issue": (it.get("issue") or it.get("name") or "").strip(),
            "Severity": normalize_severity(it.get("severity")),
            "Page URL": (it.get("page_url") or it.get("page") or "").strip(),
            "Suggestion": (it.get("suggestion") or "").strip(),
        })
    return rows

# -----------------------------
# Normalization (engine output → UI schema)
# -----------------------------
def _first_present(d: Dict[str, Any], keys: List[str], default=None):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def normalize_severity(s: Any) -> str:
    """Return exactly: Critical / Medium / Low"""
    if not isinstance(s, str):
        return "Low"
    t = s.strip().lower()
    if t in ("critical", "p0", "high"):
        return "Critical"
    if t in ("medium", "med", "p1"):
        return "Medium"
    return "Low"


def normalize_result(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"pages": [], "issues": [], "score": None, "score_breakdown": {}}

    if isinstance(result, dict):
        page_results = result.get("page_results")
        if isinstance(page_results, list) and page_results:
            out["pages"] = page_results

            # flatten issues from each page_result
            flat: List[Dict[str, Any]] = []
            for pr in page_results:
                if isinstance(pr, dict):
                    its = pr.get("issues")
                    if isinstance(its, list):
                        for it in its:
                            if isinstance(it, dict):
                                it2 = dict(it)
                                it2.setdefault("page_url", pr.get("url") or pr.get("page_url") or pr.get("page"))
                                flat.append(it2)
            if flat:
                out["issues"] = flat

        # fallback keys
        pages = _first_present(result, ["pages", "crawled_pages", "page_objects", "page_list"], default=[])
        issues = _first_present(result, ["issues", "all_issues", "defects", "findings"], default=[])

        if not out["pages"]:
            out["pages"] = pages if isinstance(pages, list) else []
        if not out["issues"]:
            out["issues"] = issues if isinstance(issues, list) else []

        # score
        s = result.get("score")
        if isinstance(s, (int, float)):
            out["score"] = int(s)
        elif isinstance(s, dict):
            if isinstance(s.get("score"), (int, float)):
                out["score"] = int(s["score"])
            if isinstance(s.get("breakdown"), dict):
                out["score_breakdown"] = s["breakdown"]

        # summary score preferred
        summary = result.get("summary")
        if isinstance(summary, dict):
            avg = summary.get("site_hygiene_score_avg")
            if isinstance(avg, (int, float)):
                out["score"] = int(avg)

        bd2 = result.get("breakdown") or result.get("score_breakdown")
        if isinstance(bd2, dict) and not out["score_breakdown"]:
            out["score_breakdown"] = bd2

    # fallback: latest run artifacts
    if not out["pages"] and not out["issues"] and out["score"] is None:
        run_dir = find_latest_run_dir()
        if run_dir:
            out["pages"] = safe_read_json(run_dir / "pages.json", [])
            out["issues"] = safe_read_json(run_dir / "issues.json", [])
            score_obj = safe_read_json(run_dir / "score.json", {})
            if isinstance(score_obj, dict):
                sc = score_obj.get("score")
                if isinstance(sc, (int, float)):
                    out["score"] = int(sc)
                bd = score_obj.get("breakdown")
                if isinstance(bd, dict):
                    out["score_breakdown"] = bd

    # normalize severity
    normalized_issues = []
    for it in out["issues"] or []:
        if isinstance(it, dict):
            it2 = dict(it)
            it2["severity"] = normalize_severity(it2.get("severity"))
            normalized_issues.append(it2)
    out["issues"] = normalized_issues

    return out


# -----------------------------
# Scoring (fallback only)
# -----------------------------
def score_from_issues(issues: List[Dict[str, Any]]) -> tuple[int, dict]:
    counts = {"Critical": 0, "Medium": 0, "Low": 0}
    for it in issues or []:
        counts[normalize_severity(it.get("severity"))] += 1

    c, m, l = counts["Critical"], counts["Medium"], counts["Low"]
    raw = 100 * math.exp(-(0.08 * c + 0.03 * m + 0.01 * l))
    score = int(round(max(0, min(100, raw))))
    return score, {"counts": counts, "formula": "score = 100 * exp(-(0.08*C + 0.03*M + 0.01*L))", "raw": raw}


def compute_counts(issues: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"Critical": 0, "Medium": 0, "Low": 0}
    for it in issues or []:
        counts[normalize_severity(it.get("severity"))] += 1
    return counts


# -----------------------------
# UI-side Dedupe + Suggestions
# -----------------------------
def dedupe_issues_for_ui(
    issues: List[Dict[str, Any]],
    mode: str = "type",  # "type" or "strict"
) -> List[Dict[str, Any]]:
    """
    type   -> one row per defect type (best for dashboards, avoids Console Error spam)
    strict -> dedupe exact duplicates only (uses issue_id if present)
    """
    seen = set()
    out = []

    for it in issues or []:
        issue = (it.get("issue") or it.get("name") or "").strip().lower()
        sev = normalize_severity(it.get("severity"))
        hint = (it.get("hint") or "").strip().lower()
        iid = (it.get("issue_id") or "").strip()

        if mode == "strict":
            key = ("id", iid) if iid else ("k", issue, sev, hint)
        else:
            # ✅ collapse all Console Error instances into ONE row
            key = ("type", issue, sev)

        if key in seen:
            continue
        seen.add(key)
        out.append(it)

    return out

def suggestions_ranked(issues: List[Dict[str, Any]]) -> List[str]:
    """
    Unique suggestions ordered by frequency.
    """
    agg: Dict[str, int] = {}
    for it in issues or []:
        s = (it.get("suggestion") or "").strip()
        if not s:
            continue
        agg[s] = agg.get(s, 0) + 1
    return [s for s, _ in sorted(agg.items(), key=lambda x: x[1], reverse=True)]


# -----------------------------
# UI Config + CSS
# -----------------------------
st.set_page_config(page_title="Bug Hygiene Engine", page_icon="🐞", layout="wide", initial_sidebar_state="expanded")

st.markdown(
    """
<style>
div[data-testid="stToolbar"] { display: none !important; }
#MainMenu { visibility: hidden; }
footer { visibility: hidden; }

/* ---------- Sidebar widget text fixes ---------- */

/* Radio label text (Overview/Bugs/Reports/Screenshots) */
section[data-testid="stSidebar"] div[role="radiogroup"] label,
section[data-testid="stSidebar"] div[role="radiogroup"] label * {
  color: #f9fafb !important;
  font-weight: 800 !important;
}

/* Radio "Navigation" title */
section[data-testid="stSidebar"] .stRadio > label,
section[data-testid="stSidebar"] .stRadio > label * {
  color: #f9fafb !important;
  font-weight: 900 !important;
}

/* Sidebar button text ("← Home") */
section[data-testid="stSidebar"] .stButton button,
section[data-testid="stSidebar"] .stButton button * {
  color: #111827 !important;  /* dark text on white button */
  font-weight: 900 !important;
}

/* ✅ Remove the top white header strip */
header[data-testid="stHeader"]{
  background: transparent !important;
}

/* 🚫 Remove sidebar collapse arrow */
button[data-testid="collapsedControl"] {
  display: none !important;
}

/* Streamlit sometimes adds a background behind the main container */
.stAppViewContainer{
  background: transparent !important;
}

/* Remove default top padding that can look like a strip */
.block-container{
  padding-top: 1.2rem !important;
}

@keyframes glowMove {
  0% { background-position: 0% 0%; }
  100% { background-position: 100% 100%; }
}

.stApp {
  background: 
    radial-gradient(circle at 15% 20%, rgba(59,130,246,0.35), transparent 40%),
    radial-gradient(circle at 85% 80%, rgba(249,115,22,0.30), transparent 45%),
    linear-gradient(135deg, #0f172a 0%, #0b1220 100%) !important;
  background-size: 200% 200%;
  animation: glowMove 20s ease infinite alternate;
}

/* 🌑 Sidebar matches background (glassy) */
section[data-testid="stSidebar"]{
  background: rgba(15, 23, 42, 0.72) !important;  /* matches your main bg */
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border-right: 1px solid rgba(255,255,255,0.06) !important;
}
section[data-testid="stSidebar"] * { color: #f9fafb !important; }

/* ✅ Remove default inner sidebar background layer */
section[data-testid="stSidebar"] > div {
  background: transparent !important;
}

/* Sidebar text */
section[data-testid="stSidebar"] * {
  color: #f9fafb !important;
}

/* ✅ Force sidebar visible */
section[data-testid="stSidebar"] {
  transform: none !important;
  margin-left: 0 !important;
  left: 0 !important;
  visibility: visible !important;
  display: block !important;
  width: 20rem !important;
  min-width: 20rem !important;
}



/* ✅ Force main content to leave space for sidebar */
div[data-testid="stAppViewContainer"] .main {
  margin-left: 20rem !important;
}

a, a:visited { color: #2563eb !important; text-decoration: none !important; }
a:hover { text-decoration: underline !important; }

label, .stTextInput label, .stTextInput p{
  color: #374151 !important;
  font-weight: 800 !important;
}

label, .stTextInput label, .stTextInput p{
  color: #374151 !important;   /* dark */
}

.statusline{ 
  color: #111827 !important;   /* very dark */
}

/* ✅ Make labels + normal text visible on dark background */
label,
.stTextInput label,
.stTextInput p,
.stMarkdown,
.stMarkdown p,
.stCaption,
.stAlert,
div[data-testid="stStatusWidget"] * {
  color: #e5e7eb !important;   /* light gray */
  font-weight: 700 !important;
}

/* ✅ Your status text like "Crawling ..." */
.statusline,
div[data-testid="stText"] {
  color: #e5e7eb !important;
  font-size: 14px !important;
  font-weight: 800 !important;
}

/* Optional: make small helper text slightly dimmer */
small, .stCaption {
  color: #cbd5e1 !important;
} 


div[data-baseweb="input"] input, .stTextInput input{
  background: #ffffff !important;
  border: 1px solid #d1d5db !important;
  color: #111827 !important;
  -webkit-text-fill-color: #111827 !important;
  border-radius: 12px !important;
  padding: 10px 12px !important;
}
div[data-baseweb="input"] input:focus, .stTextInput input:focus{
  outline: none !important;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.18) !important;
  border-color: rgba(37, 99, 235, 0.65) !important;
}
div[data-baseweb="input"] input:disabled, .stTextInput input:disabled{
  background: #f9fafb !important;
  color: #111827 !important;
  -webkit-text-fill-color: #111827 !important;
  opacity: 1 !important;
}

.stButton button{
  border-radius: 12px !important;
  padding: 10px 14px !important;
  border: 1px solid #d1d5db !important;
  background: #ffffff !important;
  color: #111827 !important;
  font-weight: 900 !important;
}
.stButton button:hover{
  background: #f9fafb !important;
  border-color: #cbd5e1 !important;
}

.card, .panel, .listcard{
  background: rgba(17, 24, 39, 0.85);
  border: 1px solid rgba(255,255,255,0.05);
  border-radius: 18px;
  padding: 16px;
  backdrop-filter: blur(14px);
  box-shadow: 0 20px 40px rgba(0,0,0,0.4);
}

.bigtitle{ font-size: 56px; font-weight: 950; color: #111827; line-height: 1.03; }
.pageTitle, .bigtitle, .listcard-title {
  color: #f9fafb !important;
}

.muted {
  color: #9ca3af !important;
}

.listtitle {
  color: #f3f4f6 !important;
}

.listsub {
  color: #9ca3af !important;
}

.kpi-label{
  color: #cbd5e1 !important;
  font-size: 12px !important;
  font-weight: 900 !important;
  letter-spacing: 0.02em;
}

.kpi-value{ font-size: 30px; font-weight: 950; line-height: 1; }
.kpi-hint{ margin-top: 6px; font-size: 12px; color: #6b7280; }

.pill {
  border-radius: 999px;
  font-weight: 800;
  padding: 6px 12px;
}

.pill-critical{
  background: rgba(249,115,22,0.18);
  border: 1px solid rgba(249,115,22,0.4);
  color: #fb923c;
}

.pill-medium{
  background: rgba(59,130,246,0.18);
  border: 1px solid rgba(59,130,246,0.4);
  color: #60a5fa;
}

.pill-low{
  background: rgba(148,163,184,0.15);
  border: 1px solid rgba(148,163,184,0.3);
  color: #cbd5e1;
}


.listcard-title {
  font-size: 16px;
  font-weight: 950;
  color: #111827;
  margin: 0;
}
.listcard-sub {
  margin-top: 4px;
  font-size: 12px;
  color: #f3f4f6;
}
.listwrap {
  margin-top: 12px;
  border-top: 1px solid #eef2f7;
}
.listrow {
  display:flex;
  justify-content:space-between;
  align-items:center;
  gap: 12px;
  padding: 12px 6px;
}
.listrow:hover{ 
  background: rgba(255,255,255,0.05); 
}.divider {
  height: 1px;
  background: #eef2f7;
  margin: 0 6px;
}


.ins-bullets { margin-top: 10px; border-top: 1px solid #eef2f7; padding-top: 10px; }
.ins-item { display:flex; gap:10px; padding: 10px 0; }
.ins-dot {
  width: 8px; height: 8px; border-radius: 999px;
  background: #2563eb; margin-top: 6px; flex: 0 0 auto;
}
.ins-text { color:#e5e7eb; font-size:13px; line-height:1.4; }

.pb-grid{ display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 1100px){ .pb-grid{ grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 650px){ .pb-grid{ grid-template-columns: 1fr; } }
.pb-item{ padding: 12px; border-radius: 14px; border: 1px solid #e5e7eb; background: #ffffff; }
.pb-num{ font-size: 22px; font-weight: 950; color: #111827; }
.pb-label{ margin-top: 2px; font-size: 12px; color: #6b7280; }

.statusline{ color: #f9fafb !important; font-size: 14px; font-weight: 700; }
</style>
""",
    unsafe_allow_html=True,
)


# -----------------------------
# State
# -----------------------------
def init_state():
    st.session_state.setdefault("route", "landing")  # landing | dashboard
    st.session_state.setdefault("url", "")
    st.session_state.setdefault("nav", "Overview")
    st.session_state.setdefault("result", None)
    st.session_state.setdefault("raw_result", None)
    st.session_state.setdefault("selected_bug_key", None)


init_state()


# -----------------------------
# HTML Builders (IMPORTANT: no indentation -> no code blocks)
# -----------------------------
def pill_class(sev: str) -> str:
    sev = normalize_severity(sev)
    if sev == "Critical":
        return "pill pill-critical"
    if sev == "Medium":
        return "pill pill-medium"
    return "pill pill-low"


def _esc(x: str) -> str:
    return (x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def build_bug_table_html(title: str, rows: List[Dict[str, Any]], empty_text: str) -> str:
    if not rows:
        html = f"""
<div class="listcard">
  <div class="listcard-title">{_esc(title)}</div>
  <div class="listcard-sub">{_esc(empty_text)}</div>
</div>
"""
        return textwrap.dedent(html).strip()

    body = []
    for idx, it in enumerate(rows):
        issue = _esc((it.get("issue") or it.get("name") or "Issue").strip())
        page = _esc((it.get("page_url") or it.get("page") or "").strip())
        sev = normalize_severity(it.get("severity"))
        sugg = _esc((it.get("suggestion") or "").strip() or "—")

        div = "" if idx == len(rows) - 1 else '<div class="divider"></div>'

        body.append(
    f"""
<div style="display:grid;grid-template-columns: 2.2fr 1.2fr 0.8fr 2.8fr;
            gap:18px;align-items:center;padding:14px 6px;">
  <div style="min-width:0;">
    <div style="font-weight:900;color:#f9fafb;font-size:15px;
                white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
      {issue}
    </div>
  </div>

  <div style="min-width:0;">
    <div style="font-size:12px;color:#cbd5e1;
                white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
      {page}
    </div>
  </div>

  <div style="display:flex;justify-content:flex-end;align-items:center;">
    <div class="{pill_class(sev)}">{sev}</div>
  </div>

  <div style="min-width:0;padding-left:4px;">
    <div style="font-size:12px;color:#e5e7eb;line-height:1.35; 
                display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
                overflow:hidden;">
      {sugg}
    </div>
  </div>
</div>
{div}
"""
)


    html = f"""
<div class="listcard">
  <div style="display:flex;justify-content:space-between;align-items:flex-end;gap:10px;">
    <div class="listcard-title">{_esc(title)}</div>
  </div>

  <div style="margin-top:10px;border-top:1px solid #eef2f7;padding-top:10px;">
    <div style="display:grid;grid-template-columns: 2.1fr 1.4fr 0.7fr 2.6fr;gap:14px;padding:0 6px 10px 6px;">
      <div class="kpi-label">Bug</div>
      <div class="kpi-label">Page</div>
      <div class="kpi-label" style="text-align:right;">Severity</div>
      <div class="kpi-label">Insight</div>
    </div>
    {''.join(body)}
  </div>
</div>
"""
    return textwrap.dedent(html).strip()

def build_list_card_html(title: str, rows: List[Dict[str, Any]], empty_text: str) -> str:
    if not rows:
        html = f"""
<div class="listcard">
  <div class="listcard-title">{_esc(title)}</div>
  <div class="listcard-sub">{_esc(empty_text)}</div>
</div>
"""
        return textwrap.dedent(html).strip()

    body = []
    for idx, it in enumerate(rows):
        t = _esc(it.get("issue") or it.get("name") or "Issue")
        p = _esc(it.get("page_url") or it.get("page") or "")
        sev = normalize_severity(it.get("severity"))
        div = "" if idx == len(rows) - 1 else '<div class="divider"></div>'

        body.append(
            f"""
<div class="listrow">
  <div style="min-width:0;">
    <div class="listtitle">{t}</div>
    <div class="listsub">{p}</div>
  </div>
  <div class="{pill_class(sev)}">{sev}</div>
</div>
{div}
"""
        )

    html = f"""
<div class="listcard">
  <div class="listcard-title">{_esc(title)}</div>
  <div class="listwrap">
    {''.join(body)}
  </div>
</div>
"""
    return textwrap.dedent(html).strip()


def build_insights_card_html(title: str, suggestions: List[str]) -> str:
    if not suggestions:
        html = f"""
<div class="listcard">
  <div class="listcard-title">{_esc(title)}</div>
  <div class="listcard-sub" style="margin-top:10px;">No suggestions available yet.</div>
</div>
"""
        return textwrap.dedent(html).strip()

    items = []
    for s in suggestions:
        items.append(
            f"""
<div class="ins-item">
  <div class="ins-dot"></div>
  <div class="ins-text">{_esc(s)}</div>
</div>
"""
        )

    html = f"""
<div class="listcard">
  <div class="listcard-title">{_esc(title)}</div>
  <div class="ins-bullets">
    {''.join(items)}
  </div>
</div>
"""
    return textwrap.dedent(html).strip()


# -----------------------------
# UI pieces
# -----------------------------
def kpi_card(label, value, hint="", color="#111827"):
    st.markdown(
        f"""
<div class="card">
  <div class="kpi-label">{_esc(str(label))}</div>
  <div class="kpi-value" style="color:{color};">{_esc(str(value))}</div>
  <div class="kpi-hint">{_esc(str(hint))}</div>
</div>
""",
        unsafe_allow_html=True,
    )


def recent_bugs_panel(issues: List[Dict[str, Any]]):
    order = {"Critical": 0, "Medium": 1, "Low": 2}
    issues_u = dedupe_issues_for_ui(issues, mode = "type")
    recent = sorted(issues_u, key=lambda it: order.get(normalize_severity(it.get("severity")), 9))[:5]

    html = build_list_card_html("Recent Bugs", recent, "Run analysis to see issues here.")
    st.markdown(html, unsafe_allow_html=True)


def priority_breakdown_panel(issues: List[Dict[str, Any]]):
    counts = compute_counts(issues)
    st.markdown(
        f"""
<div class="panel">
  <div class="listcard-title" style="margin-bottom:10px;">Priority Breakdown</div>
  <div class="pb-grid">
    <div class="pb-item">
      <div class="pb-num">{counts["Critical"]}</div>
      <div class="pb-label">Critical</div>
    </div>
    <div class="pb-item">
      <div class="pb-num">{counts["Medium"]}</div>
      <div class="pb-label">Medium</div>
    </div>
    <div class="pb-item">
      <div class="pb-num">{counts["Low"]}</div>
      <div class="pb-label">Low</div>
    </div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def insights_panel(issues: List[Dict[str, Any]], top_k: Optional[int] = 3):
    counts = compute_counts(issues)

    suggs_all = suggestions_ranked(issues)
    suggs = suggs_all[:top_k] if isinstance(top_k, int) else suggs_all

    html = build_insights_card_html("Insights", suggs)
    st.markdown(html, unsafe_allow_html=True)


# -----------------------------
# Pages
# -----------------------------
def landing_page():
    st.markdown('<div style="font-size:70px;">🐞</div>', unsafe_allow_html=True)
    st.markdown('<div class="bigtitle">BUG Hygiene Engine</div>', unsafe_allow_html=True)
    st.markdown('<div style="height:18px;"></div>', unsafe_allow_html=True)

    st.session_state.url = st.text_input("Enter website URL", value=st.session_state.url, placeholder="")

    if st.button("Analyze →", use_container_width=True):
        url = (st.session_state.url or "").strip()
        if not url:
            st.error("Please enter a URL.")
            return

        progress = st.progress(0)
        status = st.empty()

        progress.progress(5)
        status.markdown("<div class='statusline'>Starting…</div>", unsafe_allow_html=True)

        def progress_cb(_p, msg):
            # ONLY update text; ignore engine percent to prevent jumpy bar
            if msg:
                status.markdown(f"<div class='statusline'>{_esc(msg)}</div>", unsafe_allow_html=True)

        try:
            progress.progress(20)
            status.markdown("<div class='statusline'>Crawling pages…</div>", unsafe_allow_html=True)

            raw = run_async(run_engine(url, max_pages=CRAWL_BUDGET, progress_cb=progress_cb))

            progress.progress(70)
            status.markdown("<div class='statusline'>Analyzing issues…</div>", unsafe_allow_html=True)

            norm = normalize_result(raw)

            progress.progress(85)
            status.markdown("<div class='statusline'>Scoring hygiene…</div>", unsafe_allow_html=True)

            if norm.get("score") is None:
                sc, bd = score_from_issues(norm.get("issues") or [])
                norm["score"] = sc
                norm["score_breakdown"] = bd

            progress.progress(100)
            status.markdown("<div class='statusline'>Done ✅</div>", unsafe_allow_html=True)

            st.session_state.raw_result = raw
            st.session_state.result = norm
            st.session_state.nav = "Overview"
            st.session_state.route = "dashboard"
            st.rerun()
        except Exception as e:
            status.error(f"Analysis failed: {e}")


def overview_view(result: Dict[str, Any]):
    issues = result.get("issues") or []
    issues_u = dedupe_issues_for_ui(issues, mode="type")  # one place

    left, right = st.columns([1.65, 1], gap="large")

    with left:
        recent_bugs_panel(issues_u)
        st.markdown('<div style="height:12px;"></div>', unsafe_allow_html=True)
        priority_breakdown_panel(issues_u)

    with right:
        insights_panel(issues_u, top_k=3)


def bugs_view(result: Dict[str, Any]):
    issues = result.get("issues") or []
    order = {"Critical": 0, "Medium": 1, "Low": 2}

    issues_u = dedupe_issues_for_ui(issues, mode="type")
    issues_u = sorted(issues_u, key=lambda it: order.get(normalize_severity(it.get("severity")), 9))

    html = build_bug_table_html("Bug List", issues_u, "No issues found.")
    st.markdown(html, unsafe_allow_html=True)

def insights_view(result: Dict[str, Any]):
    issues = result.get("issues") or []
    # show ALL suggestions on Insights page
    insights_panel(issues, top_k=None)


def reports_view(result: Dict[str, Any]):
    st.markdown(
        """
        <div class="card">
        <h3 style="margin:0 0 6px 0;color:#f9fafb;">Reports</h3>
        <div class="muted">Download issues as CSV or Excel.</div>   
        </div>
        """,
        unsafe_allow_html=True,
    )

    issues = result.get("issues") or []

    # ✅ Choose: unique per defect type OR full list
    # If you want no repeats in the report too:
    issues_for_report = dedupe_issues_for_ui(issues, mode="type")
    # If you want full (but dedup exact duplicates only):
    # issues_for_report = dedupe_issues_for_ui(issues, mode="strict")

    rows = build_report_rows(issues_for_report)
    if not rows:
        st.info("No issues to export.")
        return

    df = pd.DataFrame(rows)

    # ---------- CSV ----------
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        data=csv_bytes,
        file_name="bug_hygiene_report.csv",
        mime="text/csv",
        use_container_width=True,
    )

    # ---------- Excel ----------
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Issues")
    out.seek(0)

    st.download_button(
        "Download Excel (.xlsx)",
        data=out.getvalue(),
        file_name="bug_hygiene_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    # optional preview
    st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)
    st.dataframe(df, use_container_width=True, hide_index=True)


def screenshots_view(result: Dict[str, Any]):
    st.markdown(
        """
        <div class="card">
          <h3 style="margin:0 0 6px 0;color:#f9fafb;">Screenshots</h3>
          <div class="muted">Shows evidence screenshots saved during crawling/analysis.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    run_dir = find_latest_run_dir()
    paths: List[str] = []

    # 1) From issues (if present)
    issues = result.get("issues") or []
    for it in issues:
        p = it.get("screenshot_path") or it.get("screenshot") or it.get("evidence_screenshot")
        if isinstance(p, str) and p.lower().endswith((".png", ".jpg", ".jpeg")):
            paths.append(p)

    # 2) From latest run folder (✅ correct folder name: "screens")
    if run_dir:
        screens_dir = run_dir / "screens"
        if screens_dir.exists():
            for fp in sorted(list(screens_dir.glob("*.png")) + list(screens_dir.glob("*.jpg")) + list(screens_dir.glob("*.jpeg"))):
                paths.append(str(fp))

    # Unique preserve order
    seen = set()
    uniq = []
    for p in paths:
        if p not in seen:
            uniq.append(p)
            seen.add(p)
    paths = uniq

    if not paths:
        st.info("No screenshots found yet.")
        return

    cols = st.columns(3)
    for i, p in enumerate(paths):
        with cols[i % 3]:
            try:
                st.image(p, use_container_width=True)
                st.caption(Path(p).name)
            except Exception:
                st.caption(f"Could not load: {p}")

def dashboard_shell():
    result = st.session_state.result
    if not isinstance(result, dict):
        result = {"pages": [], "issues": [], "score": None, "score_breakdown": {}}

    issues = result.get("issues") or []
    pages = result.get("pages") or []

    issues_all = result.get("issues") or []
    
    if result.get("score") is None:
        sc, bd = score_from_issues(issues)
        result["score"] = sc
        result["score_breakdown"] = bd

    # deduped for UI counts
    issues_u = dedupe_issues_for_ui(issues_all, mode="type")
    counts = compute_counts(issues_u)

    with st.sidebar:
        st.markdown(
            """
<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
  <div style="font-size:26px;">🐞</div>
  <div style="font-weight:950;font-size:18px;line-height:1;">Bug Hygiene</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.text_input("Target URL", value=st.session_state.url or "", disabled=True)
        st.markdown("---")

        st.session_state.nav = st.radio(
            "Navigation",
            ["Overview", "Bugs", "Reports"],
            index=["Overview", "Bugs", "Reports"].index(st.session_state.nav),
        )

        st.markdown("---")
        if st.button("← Home", use_container_width=True):
            st.session_state.route = "landing"
            st.rerun()

        st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="muted" style="font-size:12px;">Last updated: {datetime.now().strftime("%b %d, %Y %H:%M")}</div>',
            unsafe_allow_html=True,
        )

    st.markdown(f"<div class='pageTitle'>{st.session_state.nav}</div>", unsafe_allow_html=True)
    st.markdown("<div style='height:14px;'></div>", unsafe_allow_html=True)

    k1, k2, k3, k4 = st.columns(4, gap="large")
    with k1:
        kpi_card("Pages Crawled", str(len(pages)), "Crawl budget", color="#16a34a")
    with k2:
        kpi_card("Open Bugs", str(len(issues_u)), "Unique issues", color="#f9fafb")
    with k3:
        kpi_card("Critical Issues", str(counts["Critical"]), "Needs immediate action", color="#ef4444")
    with k4:
        kpi_card("Hygiene Score", str(result.get("score", "—")), "Overall health", color="#2563eb")

    st.markdown("<div style='height:14px;'></div>", unsafe_allow_html=True)

    if st.session_state.nav == "Overview":
        overview_view(result)
    elif st.session_state.nav == "Bugs":
        bugs_view(result)
    elif st.session_state.nav == "Reports":
        reports_view(result)
    elif st.session_state.nav == "Screenshots":
        screenshots_view(result)
        


# -----------------------------
# Router
# -----------------------------
if st.session_state.route == "landing":
    landing_page()
else:
    dashboard_shell()