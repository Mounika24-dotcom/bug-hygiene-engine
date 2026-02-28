from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from playwright.sync_api import Page, Request, Response


def _is_xhr_like(req: Request) -> bool:
    rt = (req.resource_type or "").lower()
    # Playwright resource_type can be: document, stylesheet, image, media, font, script, xhr, fetch, websocket, other
    return rt in {"xhr", "fetch", "websocket"}


def _same_domain(url: str, base_url: str) -> bool:
    try:
        return urlparse(url).netloc == urlparse(base_url).netloc
    except Exception:
        return False


@dataclass
class NetworkLog:
    base_url: str
    # keep it small: store only what we need for defects
    requests: List[Dict[str, Any]] = field(default_factory=list)
    responses: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)

    def attach(self, page: Page) -> None:
        # request start
        def on_request(req: Request) -> None:
            self.requests.append(
                {
                    "url": req.url,
                    "method": req.method,
                    "resource_type": req.resource_type,
                    "is_xhr": _is_xhr_like(req),
                }
            )

        # response received
        def on_response(resp: Response) -> None:
            req = resp.request
            self.responses.append(
                {
                    "url": resp.url,
                    "status": resp.status,
                    "ok": resp.ok,
                    "resource_type": req.resource_type,
                    "is_xhr": _is_xhr_like(req),
                }
            )

        # request failed
        def on_request_failed(req: Request) -> None:
            self.failed.append(
                {
                    "url": req.url,
                    "method": req.method,
                    "resource_type": req.resource_type,
                    "is_xhr": _is_xhr_like(req),
                    "failure": (req.failure or {}).get("errorText") if req.failure else "unknown",
                }
            )

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)

    def to_issues(self, page_url: str) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []

        # A) XHR/fetch failures (4xx/5xx)
        for r in self.responses:
            if not r.get("is_xhr"):
                continue
            status = int(r.get("status") or 0)
            if status >= 400:
                sev = "High" if status >= 500 else "High"  # API 4xx also usually high for user flows
                tag = "functional"
                if status in (401, 403):
                    tag = "functional"  # auth-related, but keep in functional for scoring
                issues.append(
                    {
                        "issue": "API Failure",
                        "severity": sev,
                        "tag": "functional",
                        "locator": "network",
                        "hint": f"{status} {r.get('url')}",
                        "suggestion": "Investigate backend/API error; verify endpoint, auth, and response mapping.",
                    }
                )

        # B) Unauthorized API calls
        for r in self.responses:
            if not r.get("is_xhr"):
                continue
            status = int(r.get("status") or 0)
            if status in (401, 403):
                issues.append(
                    {
                        "issue": "Unauthorized API Call",
                        "severity": "High",
                        "tag": "functional",
                        "locator": "network",
                        "hint": f"{status} {r.get('url')}",
                        "suggestion": "Check authentication/authorization; ensure tokens/cookies are valid and sent.",
                    }
                )

        # C) Request failures (DNS, net::ERR, aborted)
        for f in self.failed:
            if not f.get("is_xhr"):
                continue
            issues.append(
                {
                    "issue": "Network Request Failed",
                    "severity": "High",
                    "tag": "functional",
                    "locator": "network",
                    "hint": f"{f.get('failure')} {f.get('url')}",
                    "suggestion": "Check connectivity/CORS/DNS; ensure endpoint is reachable and not blocked.",
                }
            )

        return issues


def run_network_api_detector(page: Page, base_url: str, page_url: str) -> List[Dict[str, Any]]:
    """
    Usage:
      netlog = NetworkLog(base_url); netlog.attach(page)
      ... navigate ...
      issues = netlog.to_issues(page.url)
    This helper is here if you want a single call style.
    """
    # This function assumes you've already attached + navigated; otherwise it returns nothing useful.
    return []