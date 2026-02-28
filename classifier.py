from bs4 import BeautifulSoup
from urllib.parse import urlparse
import re

class PageTypeClassifier:
    def classify_page_type(self, dom: str = "", url: str = "", title: str = "") -> str:
        soup = BeautifulSoup(dom or "", "html.parser")
        text = soup.get_text(" ", strip=True).lower()

        path = (urlparse(url).path or "").lower()
        title_l = (title or "").lower()

        # --- URL route rules (your dummy site needs these) ---
        if path in ("", "/"):
            return "home"
        if "/menu" in path:
            return "listing"
        if "/cart" in path:
            return "cart"
        if "/orders" in path:
            return "orders"
        if "/admin" in path:
            return "admin"
        if re.search(r"/product/", path):
            return "product_detail"

        # --- error pages ---
        if "404" in title_l or "not found" in title_l:
            return "error_404"
        if "500" in title_l or "internal server error" in title_l:
            return "error_500"

        # --- DOM heuristics (fallback) ---
        inputs = soup.find_all(["input", "textarea", "select"])
        tables = soup.find_all("table")
        canvases = soup.find_all("canvas")

        if soup.find("form") and len(inputs) >= 2:
            return "form"
        if soup.find("input", attrs={"type": "search"}) or ("search" in text and soup.find("form")):
            return "search"
        if len(canvases) >= 1 or ("dashboard" in text) or ("kpi" in text):
            return "dashboard"
        if len(tables) >= 2:
            return "table"

        return "unknown"