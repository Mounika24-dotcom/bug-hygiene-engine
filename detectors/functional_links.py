from __future__ import annotations

from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from playwright.sync_api import APIRequestContext


def _same_domain(url: str, base_url: str) -> bool:
    try:
        return urlparse(url).netloc == urlparse(base_url).netloc
    except Exception:
        return False


def _is_http(url: str) -> bool:
    return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))


def _follow_redirects(
    request_ctx: APIRequestContext,
    url: str,
    max_hops: int = 8,
    timeout_ms: int = 8000,
) -> Tuple[int, List[str]]:
    """
    Returns (final_status, chain_urls_including_start_and_final)
    We manually follow to detect loops/long chains.
    """
    chain = [url]
    current = url

    for _ in range(max_hops):
        resp = request_ctx.get(current, max_redirects=0, timeout=timeout_ms)
        status = resp.status

        # 3xx redirect
        if status in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location") or resp.headers.get("Location")
            if not loc:
                return status, chain
            # Resolve relative redirects
            if loc.startswith("/"):
                parsed = urlparse(current)
                loc = f"{parsed.scheme}://{parsed.netloc}{loc}"

            if loc in chain:
                chain.append(loc)
                return 310, chain  # custom code to indicate loop
            chain.append(loc)
            current = loc
            continue

        return status, chain

    # too many hops
    return 399, chain


def detect_link_defects(
    request_ctx: APIRequestContext,
    base_url: str,
    links: List[str],
    timeout_ms: int = 8000,
) -> List[Dict[str, Any]]:
    """
    links: list of absolute http(s) urls (already normalized)
    """
    issues: List[Dict[str, Any]] = []

    for u in links:
        if not _is_http(u):
            continue

        status, chain = _follow_redirects(request_ctx, u, max_hops=8, timeout_ms=timeout_ms)

        # redirect loop
        if status == 310:
            issues.append(
                {
                    "issue": "Redirect Loop",
                    "severity": "High",
                    "tag": "functional",
                    "locator": "a[href]",
                    "hint": " → ".join(chain[-5:]),
                    "suggestion": "Fix redirect rules to avoid cyclic redirects.",
                }
            )
            continue

        # redirect chain too long
        if status == 399:
            issues.append(
                {
                    "issue": "Redirect Chain Too Long",
                    "severity": "Medium",
                    "tag": "functional",
                    "locator": "a[href]",
                    "hint": f"Hops={len(chain)-1} {chain[0]}",
                    "suggestion": "Reduce redirect hops; point links directly to the final destination.",
                }
            )
            continue

        # broken link
        if status >= 400:
            sev = "High" if status >= 500 else "High"
            tag = "functional"

            # external link blocked is usually medium unless it's critical
            if not _same_domain(u, base_url):
                if status in (401, 403, 451):
                    sev = "Medium"

            issues.append(
                {
                    "issue": "Broken Link",
                    "severity": sev,
                    "tag": tag,
                    "locator": "a[href]",
                    "hint": f"{status} {u}",
                    "suggestion": "Fix href target or server route; ensure destination returns 2xx/3xx.",
                }
            )
            continue

        # external blocked (nice to flag separately)
        if (not _same_domain(u, base_url)) and status in (401, 403, 451):
            issues.append(
                {
                    "issue": "External Link Blocked",
                    "severity": "Medium",
                    "tag": "functional",
                    "locator": "a[href]",
                    "hint": f"{status} {u}",
                    "suggestion": "Confirm external destination allows access; update link or remove if restricted.",
                }
            )

    return issues