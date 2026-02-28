from __future__ import annotations

from typing import Any, Dict, List


def detect_seo_a11y_content(page) -> List[Dict[str, Any]]:
    """
    Requires page already loaded.
    Returns issues in your compact format.
    """
    data = page.evaluate(
        """
        () => {
          const title = document.title || "";
          const metaDesc = document.querySelector('meta[name="description"]')?.getAttribute("content") || "";
          const canonical = document.querySelector('link[rel="canonical"]')?.getAttribute("href") || "";

          // images missing alt (ignore hidden)
          const imgs = Array.from(document.querySelectorAll("img")).slice(0, 200);
          const missingAlt = imgs
            .filter(img => {
              const style = window.getComputedStyle(img);
              if (style.display === "none" || style.visibility === "hidden") return false;
              const alt = img.getAttribute("alt");
              return alt === null || alt.trim() === "";
            })
            .map(img => img.getAttribute("src") || "");

          // empty headings
          const headings = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6")).slice(0, 200);
          const emptyHeadings = headings
            .filter(h => (h.textContent || "").trim().length === 0)
            .map(h => h.tagName.toLowerCase());

          // lorem / placeholder text quick scan
          const bodyText = (document.body?.innerText || "").slice(0, 20000).toLowerCase();
          const hasLorem = bodyText.includes("lorem ipsum") || bodyText.includes("placeholder");

          // heading hierarchy check (simple)
          const hasH1 = !!document.querySelector("h1");

          return { title, metaDesc, canonical, missingAlt, emptyHeadings, hasLorem, hasH1 };
        }
        """
    )

    issues: List[Dict[str, Any]] = []

    # SEO
    if not (data.get("title") or "").strip():
        issues.append(
            {
                "issue": "Missing Page Title",
                "severity": "High",
                "tag": "seo",
                "locator": "title",
                "hint": "",
                "suggestion": 'Add a meaningful <title> tag (50–60 chars) describing the page.',
            }
        )

    if not (data.get("metaDesc") or "").strip():
        issues.append(
            {
                "issue": "Missing Meta Description",
                "severity": "Medium",
                "tag": "seo",
                "locator": "meta",
                "hint": "name=description",
                "suggestion": "Add <meta name='description' content='...'> to improve SEO snippet quality.",
            }
        )

    if not (data.get("canonical") or "").strip():
        issues.append(
            {
                "issue": "Missing Canonical",
                "severity": "Low",
                "tag": "seo",
                "locator": "link[rel=canonical]",
                "hint": "",
                "suggestion": "Add a canonical URL to reduce duplicate URL/parameter issues.",
            }
        )

    # Accessibility
    missing_alt = data.get("missingAlt") or []
    for src in missing_alt[:10]:
        issues.append(
            {
                "issue": "Missing Alt Text",
                "severity": "High",
                "tag": "accessibility",
                "locator": "img",
                "hint": f"src={src}" if src else "",
                "suggestion": "Add alt text describing the image. Use alt='' only for decorative images.",
            }
        )

    if not bool(data.get("hasH1")):
        issues.append(
            {
                "issue": "Missing H1 Heading",
                "severity": "Medium",
                "tag": "accessibility",
                "locator": "h1",
                "hint": "",
                "suggestion": "Add one clear H1 per page to improve structure and screen-reader navigation.",
            }
        )

    # Content hygiene
    empty_heads = data.get("emptyHeadings") or []
    if empty_heads:
        issues.append(
            {
                "issue": "Empty Heading",
                "severity": "Low",
                "tag": "content",
                "locator": ",".join(sorted(set(empty_heads))),
                "hint": f"count={len(empty_heads)}",
                "suggestion": "Remove empty headings or add meaningful text content.",
            }
        )

    if bool(data.get("hasLorem")):
        issues.append(
            {
                "issue": "Placeholder Text Found",
                "severity": "Low",
                "tag": "content",
                "locator": "body",
                "hint": "lorem/placeholder",
                "suggestion": "Replace placeholder text with real content.",
            }
        )

    return issues