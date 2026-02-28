# graph.py
import networkx as nx


class DefectGraph:
    def __init__(self):
        self.graph = nx.DiGraph()

    def build_graph(self, page_url: str, issues: list[dict]):
        self.graph.add_node(page_url, type="page")

        for issue in issues:
            locator = issue.get("locator", "") or "unknown_element"
            issue_type = issue.get("issue", "Unknown")
            severity = issue.get("severity", "Low")

            el_node = f"el:{locator}"
            type_node = f"type:{issue_type}"
            sev_node = f"sev:{severity}"

            self.graph.add_node(el_node, type="element")
            self.graph.add_node(type_node, type="issue_type")
            self.graph.add_node(sev_node, type="severity")

            self.graph.add_edge(page_url, el_node)
            self.graph.add_edge(el_node, type_node)
            self.graph.add_edge(type_node, sev_node)

        return self.graph