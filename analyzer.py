from __future__ import annotations

from bs4 import BeautifulSoup

print(">>> LOADED analyzer.py FROM:", __file__)


class Analyzer:
    def _short_locator(self, el) -> str:
        tag = getattr(el, "name", "unknown") or "unknown"

        el_id = el.get("id") if hasattr(el, "get") else None
        if el_id:
            return f"{tag}#{str(el_id)[:50]}"

        classes = el.get("class") if hasattr(el, "get") else None
        classes = classes or []
        if isinstance(classes, (list, tuple)) and classes:
            cls = ".".join([c for c in classes[:2] if isinstance(c, str) and c.strip()])
            if cls:
                return f"{tag}.{cls}"

        return tag

    def suggestion_for(self, issue: str) -> str:
        fixes = {
            "Missing Meta Description": "Add <meta name='description' content='...'> to improve SEO snippet quality.",
            "Missing Page Title": "Add a meaningful <title> tag (50–60 chars) describing the page.",
            "Missing Canonical": "Add <link rel='canonical' href='...'> to reduce duplicate URL/parameter issues.",
            "Missing Alt Text": "Add alt text that describes the image for screen readers. Use alt='' only for decorative images.",
            "Missing H1 Heading": "Add one clear H1 per page to improve structure and screen-reader navigation.",
            "Broken Link": "Fix the href, remove the link, or replace it with a valid destination URL.",
            "External Link Blocked": "Confirm external destination allows access; update link or remove if restricted.",
            "Form Input Missing Label": "Add <label for='...'> or aria-label/aria-labelledby so screen readers can identify the field.",
            "Empty Heading": "Remove empty heading tags or add meaningful heading text for structure.",
            "Placeholder Text Found": "Replace placeholder text with real content.",
            "Slow Load Time": "Reduce heavy JS, compress images, enable caching, and investigate slow network requests.",
            "Console Error": "Inspect browser console stack trace and fix JS error; often caused by missing scripts or runtime exceptions.",
            "JS Exception": "Fix the thrown error in the app code; add try/catch and validate null/undefined access.",
            "Request Failed": "Investigate failing network request (CORS/404/500). Ensure endpoint is reachable and correct.",
            "API Failure": "Investigate backend/API error; verify endpoint, auth, and response mapping.",
            "Unauthorized API Call": "Check authentication/authorization; ensure tokens/cookies are valid and sent.",
        }
        return fixes.get(issue, "Review the element and update HTML/CSS/JS to resolve the issue.")

    def _short_hint(self, el) -> str:
        if hasattr(el, "get"):
            for attr in ["href", "src", "name", "type", "aria-label", "role", "placeholder"]:
                v = el.get(attr)
                if v:
                    s = str(v).strip().replace("\n", " ")
                    return f"{attr}={s[:90]}"

        txt = el.get_text(" ", strip=True) if hasattr(el, "get_text") else ""
        if txt:
            return f"text={txt[:70]}"
        return ""

    def _evidence_snippet(self, el) -> str:
        try:
            s = " ".join(str(el).split())
            return s[:180]
        except Exception:
            return ""

    def _add_issue(self, issues: list, el, issue: str, severity: str, tag_override: str | None = None):
        issues.append(
            {
                "issue": issue,
                "severity": severity,
                "tag": tag_override if tag_override is not None else (getattr(el, "name", "") or ""),
                "locator": self._short_locator(el),
                "hint": self._short_hint(el),
                "suggestion": self.suggestion_for(issue),
                "evidence": self._evidence_snippet(el),
            }
        )

    def _add_page_issue(self, issues: list, soup: BeautifulSoup, issue: str, severity: str, hint: str, tag: str):
        issues.append(
            {
                "issue": issue,
                "severity": severity,
                "tag": tag,
                "locator": "page",
                "hint": (hint or "")[:140],
                "suggestion": self.suggestion_for(issue),
                "evidence": "",
            }
        )

    def detect_issues(self, dom: str, telemetry: dict | None = None) -> list[dict]:
        # telemetry safe
        if not isinstance(telemetry, dict):
            telemetry = {}

        # logs safe  ✅ (THIS WAS MISSING)
        logs = telemetry.get("logs") or {}
        if not isinstance(logs, dict):
            logs = {}

        soup = BeautifulSoup(dom or "", "html.parser")
        issues: list[dict] = []

        # 1) Broken links (cheap heuristics)
        for a in soup.find_all("a"):
            href = (a.get("href") or "").strip()
            if href == "" or href == "#":
                if a.get_text(strip=True):
                    self._add_issue(issues, a, "Broken Link", "Low", tag_override="functional")
                continue
            if href.lower().startswith("javascript:"):
                self._add_issue(issues, a, "Broken Link", "Medium", tag_override="functional")

        # 2) Missing alt text
        for img in soup.find_all("img"):
            if img.get("alt") is None:
                self._add_issue(issues, img, "Missing Alt Text", "Critical", tag_override="accessibility")
            elif str(img.get("alt")).strip() == "":
                self._add_issue(issues, img, "Missing Alt Text", "Low", tag_override="accessibility")

        # 3) Empty headings
        for h in soup.find_all(["h1", "h2", "h3"]):
            if not h.get_text(strip=True):
                self._add_issue(issues, h, "Empty Heading", "Low", tag_override="content")

        # 4) SEO: meta description
        if not soup.find("meta", attrs={"name": "description"}):
            fake = soup.new_tag("meta")
            fake["name"] = "description"
            self._add_issue(issues, fake, "Missing Meta Description", "Medium", tag_override="seo")

        # 5) SEO: title
        if not soup.title or not (soup.title.string and soup.title.string.strip()):
            fake = soup.new_tag("title")
            self._add_issue(issues, fake, "Missing Page Title", "Critical", tag_override="seo")

        # 6) SEO: canonical
        if not soup.find("link", attrs={"rel": "canonical"}):
            fake = soup.new_tag("link")
            fake["rel"] = "canonical"
            self._add_issue(issues, fake, "Missing Canonical", "Low", tag_override="seo")

        # 7) A11y: missing H1
        if not soup.find("h1"):
            fake = soup.new_tag("h1")
            self._add_issue(issues, fake, "Missing H1 Heading", "Medium", tag_override="accessibility")

        # 8) Placeholder scan
        body_text = (soup.get_text(" ", strip=True) or "").lower()
        if "lorem ipsum" in body_text or "placeholder" in body_text:
            self._add_page_issue(issues, soup, "Placeholder Text Found", "Low", "lorem/placeholder", "content")

        # 9) Form inputs missing label (simple heuristic)
        for inp in soup.find_all(["input", "textarea", "select"]):
            if (inp.get("type") or "").lower() == "hidden":
                continue
            has_label_signal = any([inp.get("aria-label"), inp.get("id"), inp.get("name"), inp.get("placeholder")])
            if not has_label_signal:
                self._add_issue(issues, inp, "Form Input Missing Label", "Critical", tag_override="accessibility")

        # 10) Performance check
        perf = telemetry.get("performance") or {}
        if isinstance(perf, dict):
            try:
                load_time = (perf.get("loadEventEnd", 0) - perf.get("navigationStart", 0)) or 0
                if load_time > 3000:
                    self._add_page_issue(
                        issues,
                        soup,
                        "Slow Load Time",
                        "Medium" if load_time < 6000 else "Critical",
                        f"load_ms={int(load_time)}",
                        "performance",
                    )
            except Exception:
                pass

        # Logs: dedupe console errors ✅ (prevents penalty explosion)
        seen_console = set()
        for item in logs.get("console", []) or []:
            if not isinstance(item, dict):
                continue
            if (item.get("type") or "").lower() != "error":
                continue
            txt = (item.get("text") or "")[:140]
            if txt in seen_console:
                continue
            seen_console.add(txt)
            self._add_page_issue(issues, soup, "Console Error", "Critical", txt, "functional")

        for err in logs.get("page_errors", []) or []:
            self._add_page_issue(issues, soup, "JS Exception", "Critical", str(err)[:120], "functional")

        for rf in logs.get("request_failures", []) or []:
            if isinstance(rf, dict):
                hint = ((rf.get("failure", "") or "")[:50] + " " + (rf.get("url", "") or "")[:80]).strip()
            else:
                hint = str(rf)[:120]
            self._add_page_issue(issues, soup, "Request Failed", "Medium", hint, "functional")

                # 11) Functional: Dead button / no action
        dead = telemetry.get("dead_clicks") or []
        if isinstance(dead, list):
            for d in dead[:20]:
                if not isinstance(d, dict):
                    continue
                sel = (d.get("selector") or "")[:120]
                txt = (d.get("text") or "")[:80]
                reason = (d.get("reason") or "no_change")[:40]

                hint = f"{reason} | {txt}".strip(" |")
                self._add_page_issue(
                    issues,
                    soup,
                    "Dead Button / No Action",
                    "Medium",
                    hint[:140],
                    "functional",
                )
                # If your _add_page_issue supports locator, you can upgrade later.
                # For now hint contains text, and selector is still available in telemetry.
        
        # API failures
        for a in (telemetry.get("api_failures", []) or []):
            if not isinstance(a, dict):
                continue
            status = int(a.get("status") or 0)
            if status >= 400:
                self._add_page_issue(issues, soup, "API Failure", "Critical", f"{status} {a.get('url','')}", "functional")

        for a in (telemetry.get("unauthorized_api", []) or []):
            if not isinstance(a, dict):
                continue
            status = int(a.get("status") or 0)
            self._add_page_issue(issues, soup, "Unauthorized API Call", "Critical", f"{status} {a.get('url','')}", "functional")

        # Link checks
        for lc in (telemetry.get("link_checks", []) or []):
            if not isinstance(lc, dict):
                continue
            kind = lc.get("kind")
            status = int(lc.get("status") or 0)
            u = (lc.get("url") or "")[:120]

            if kind in ("broken", "failed"):
                hint = f"{status} {u}" if status else f"failed {u}"
                self._add_page_issue(issues, soup, "Broken Link", "Critical", hint, "functional")

            if kind == "blocked":
                self._add_page_issue(issues, soup, "External Link Blocked", "Medium", f"{status} {u}", "functional")

        return issues