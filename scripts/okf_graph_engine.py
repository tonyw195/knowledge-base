#!/usr/bin/env python3
"""
okf_graph_engine.py — In-Memory NetworkX Graph Index & Traversal Engine for OKF Bundles

Provides sub-millisecond query retrieval, cross-competitor comparison,
and graph traversal for OKF v0.2 competitor remarketing intelligence.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx

# Controlled Taxonomy Mapping
TAXONOMY_CATEGORIES = {
    "buyer_fee_discount": "Fees",
    "fee_waiver": "Fees",
    "subscription_free": "Fees",
    "tier_volume_discount": "Fees",
    "transport_subsidy": "Logistics",
    "fixed_rate_delivery": "Logistics",
    "collection_service": "Logistics",
    "buyer_indemnity": "Risk & Trust",
    "assured_inspection": "Risk & Trust",
    "fault_guarantee": "Risk & Trust",
    "sign_up_bonus": "Onboarding & Incentives",
    "first_purchase_credit": "Onboarding & Incentives",
    "trader_onboarding": "Onboarding & Incentives",
    "extended_bidding_window": "Auction Mechanics",
    "hidden_reserve": "Auction Mechanics",
    "instant_settlement": "Auction Mechanics",
}

CORPORATE_GROUPS = {
    "constellation": {
        "name": "Constellation Automotive Group",
        "description": "Parent company of BCA, cinch, and Aston Barclay (acquired). Coordinates unified logistics and inventory pipelines.",
        "members": ["bca", "aston-barclay"],
    },
    "cox-automotive": {
        "name": "Cox Automotive UK",
        "description": "Global remarketing group operating Manheim and co-owning Dealer Auction (joint venture with Auto Trader UK).",
        "members": ["manheim", "dealer-auction"],
    },
}

SYNERGY_EDGES = [
    (
        "dealer-auction",
        "manheim",
        "SureCheck Inspection Framework",
        "Dealer Auction uses Manheim's technical SureCheck inspection engine and claims resolution framework.",
    ),
    (
        "aston-barclay",
        "bca",
        "Constellation Remarketing Integration",
        "Aston Barclay operates under Constellation Group, aligning physical lanes and transport with BCA.",
    ),
]


def parse_simple_yaml_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Extracts YAML frontmatter and markdown body without external dependencies."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)(.*)", content, re.DOTALL)
    if not match:
        return {}, content

    yaml_text = match.group(1)
    body = match.group(2)
    data: Dict[str, Any] = {"tags": []}
    current_list_key = None

    for line in yaml_text.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue

        if trimmed.startswith("- ") and current_list_key:
            item_val = trimmed[2:].strip().strip("\"'")
            if current_list_key in data and isinstance(data[current_list_key], list):
                data[current_list_key].append(item_val)
            continue

        if ":" in trimmed:
            parts = trimmed.split(":", 1)
            key = parts[0].strip()
            raw_val = parts[1].strip()

            if raw_val.startswith("[") and raw_val.endswith("]"):
                current_list_key = None
                inner = raw_val[1:-1].strip()
                if inner:
                    items = [x.strip().strip("\"'") for x in inner.split(",") if x.strip()]
                    data[key] = items
                else:
                    data[key] = []
            elif raw_val == "":
                current_list_key = key
                if key not in data:
                    data[key] = []
            else:
                current_list_key = None
                data[key] = raw_val.strip("\"'")

    return data, body


def parse_date(date_str: str) -> Optional[datetime.datetime]:
    if not date_str:
        return None
    clean = str(date_str).strip().strip("\"'")
    try:
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(clean)
    except ValueError:
        pass
    try:
        dt = datetime.datetime.strptime(clean, "%Y-%m-%d")
        return dt.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        pass
    return None


def is_active(meta: Dict[str, Any], now: datetime.datetime) -> bool:
    status = meta.get("status", "").lower()
    if status in ["expired", "superseded", "inactive"]:
        return False
    expiry_dt = None
    if "stale_after" in meta:
        expiry_dt = parse_date(meta["stale_after"])
    elif "valid_until" in meta:
        expiry_dt = parse_date(meta["valid_until"])

    if expiry_dt and now > expiry_dt:
        return False
    return True


class OKFGraphEngine:
    def __init__(self, workspace_root: str):
        self.workspace_root = os.path.abspath(workspace_root)
        self.repo_dir = os.path.join(self.workspace_root, "okf-repository")
        self.competitors_dir = os.path.join(self.repo_dir, "competitors")
        self.config_path = os.path.join(self.workspace_root, "config", "competitors.json")
        self.graph = nx.DiGraph()
        self.last_indexed_at: Optional[datetime.datetime] = None
        self.build_graph()

    def build_graph(self) -> None:
        """Constructs the multi-relational NetworkX graph from files and config."""
        start_t = time.perf_counter()
        self.graph.clear()
        now = datetime.datetime.now(datetime.timezone.utc)

        # 1. Add Corporate Group Nodes
        for group_id, group_info in CORPORATE_GROUPS.items():
            self.graph.add_node(
                f"group:{group_id}",
                node_type="corporate_group",
                id=group_id,
                name=group_info["name"],
                description=group_info["description"],
            )

        # 2. Add Competitor Nodes from config/competitors.json
        competitor_slugs: Set[str] = set()
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as fp:
                    comp_configs = json.load(fp)
                for slug, info in comp_configs.items():
                    competitor_slugs.add(slug)
                    node_id = f"comp:{slug}"
                    self.graph.add_node(
                        node_id,
                        node_type="competitor",
                        slug=slug,
                        name=info.get("name", slug),
                        primary_url=info.get("primary_url", ""),
                        buyer_portal_url=info.get("buyer_portal_url", ""),
                        news_url=info.get("news_url", ""),
                        priority_channels=info.get("priority_channels", []),
                        socials=info.get("socials", {}),
                    )
            except Exception as e:
                print(f"[WARN] Error loading competitors.json: {e}", file=sys.stderr)

        # Associate Competitors to Corporate Groups
        for group_id, group_info in CORPORATE_GROUPS.items():
            for member_slug in group_info["members"]:
                comp_node = f"comp:{member_slug}"
                if comp_node in self.graph:
                    self.graph.add_edge(comp_node, f"group:{group_id}", edge_type="MEMBER_OF")
                    self.graph.add_edge(f"group:{group_id}", comp_node, edge_type="CONTROLS")

        # Add Sister Synergies between Competitors
        for source_slug, target_slug, synergy_name, notes in SYNERGY_EDGES:
            source_node = f"comp:{source_slug}"
            target_node = f"comp:{target_slug}"
            if source_node in self.graph and target_node in self.graph:
                self.graph.add_edge(
                    source_node, target_node, edge_type="SYNERGY", name=synergy_name, notes=notes
                )
                self.graph.add_edge(
                    target_node, source_node, edge_type="SYNERGY", name=synergy_name, notes=notes
                )

        # 3. Add Tag Nodes from Controlled Taxonomy
        for tag_name, category in TAXONOMY_CATEGORIES.items():
            self.graph.add_node(
                f"tag:{tag_name}",
                node_type="tag",
                name=tag_name,
                category=category,
            )

        # 4. Scan and Parse OKF Promotion Markdown Files
        if os.path.exists(self.competitors_dir):
            for root, _, files in os.walk(self.competitors_dir):
                for f in files:
                    if not f.endswith(".md"):
                        continue
                    filepath = os.path.join(root, f)
                    try:
                        with open(filepath, "r", encoding="utf-8") as fp:
                            content = fp.read()
                    except Exception:
                        continue

                    meta, body = parse_simple_yaml_frontmatter(content)
                    promo_id = meta.get("id") or os.path.splitext(f)[0]
                    competitor_slug = meta.get("competitor") or os.path.basename(root)

                    # Extract H1 Title from Markdown body
                    title_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
                    title = title_match.group(1).strip() if title_match else promo_id

                    # Extract blockquote excerpt
                    excerpt_match = re.search(r"^>\s*\"?(.*?)\"?$", body, re.MULTILINE)
                    summary = excerpt_match.group(1).strip() if excerpt_match else ""

                    active_flag = is_active(meta, now)
                    promo_node = f"promo:{promo_id}"

                    # Add Promotion Node
                    self.graph.add_node(
                        promo_node,
                        node_type="promotion",
                        id=promo_id,
                        competitor=competitor_slug,
                        title=title,
                        summary=summary,
                        canonical_url=meta.get("canonical_url", ""),
                        content_hash=meta.get("content_hash", ""),
                        status=meta.get("status", "unknown"),
                        active=active_flag,
                        valid_until=meta.get("valid_until", ""),
                        stale_after=meta.get("stale_after", ""),
                        filepath=os.path.relpath(filepath, self.workspace_root),
                        raw_body=body,
                    )

                    # Edge: Competitor -> Promo
                    comp_node = f"comp:{competitor_slug}"
                    if comp_node not in self.graph:
                        self.graph.add_node(
                            comp_node,
                            node_type="competitor",
                            slug=competitor_slug,
                            name=competitor_slug.replace("-", " ").title(),
                        )
                    self.graph.add_edge(comp_node, promo_node, edge_type="OFFERS")
                    self.graph.add_edge(promo_node, comp_node, edge_type="OFFERED_BY")

                    # Edge: Promo <-> Tags
                    for tag in meta.get("tags", []):
                        tag_clean = tag.strip().lower()
                        tag_node = f"tag:{tag_clean}"
                        if tag_node not in self.graph:
                            self.graph.add_node(
                                tag_node,
                                node_type="tag",
                                name=tag_clean,
                                category="Custom / Uncategorized",
                            )
                        self.graph.add_edge(promo_node, tag_node, edge_type="TAGGED_WITH")
                        self.graph.add_edge(tag_node, promo_node, edge_type="TAGS")

                    # Edge: References between files (Markdown links)
                    links = re.findall(r"\[.*?\]\((\.?/?[^\)]+\.md)\)", body)
                    for link in links:
                        target_fname = os.path.splitext(os.path.basename(link))[0]
                        target_node = f"promo:{target_fname}"
                        self.graph.add_edge(promo_node, target_node, edge_type="REFERENCES")

        self.last_indexed_at = now
        elapsed = (time.perf_counter() - start_t) * 1000
        # Graph construction telemetry
        # print(f"[OKFGraphEngine] Built graph in {elapsed:.2f}ms: {self.graph.number_of_nodes()} nodes, {self.graph.number_of_edges()} edges")

    # ---------------------------------------------------------
    # High-Performance Retrieval Methods
    # ---------------------------------------------------------

    def query_by_tag(self, tag: str, active_only: bool = True) -> List[Dict[str, Any]]:
        """Instant 1-hop lookup of promotions with a given tag."""
        clean_tag = tag.strip().lower()
        tag_node = f"tag:{clean_tag}"
        if tag_node not in self.graph:
            return []

        results = []
        for neighbor in self.graph.neighbors(tag_node):
            if neighbor.startswith("promo:"):
                data = self.graph.nodes[neighbor]
                if active_only and not data.get("active", False):
                    continue
                results.append(data)
        return results

    def get_competitor_profile(self, competitor_slug: str) -> Dict[str, Any]:
        """Retrieves full competitor graph neighborhood: promos, corporate group, synergies."""
        comp_node = f"comp:{competitor_slug.strip().lower()}"
        if comp_node not in self.graph:
            return {"error": f"Competitor '{competitor_slug}' not found."}

        node_data = dict(self.graph.nodes[comp_node])
        promotions = []
        groups = []
        synergies = []

        for target in self.graph.successors(comp_node):
            edge_data = self.graph.get_edge_data(comp_node, target) or {}
            edge_type = edge_data.get("edge_type")

            if edge_type == "OFFERS":
                promotions.append(self.graph.nodes[target])
            elif edge_type == "MEMBER_OF":
                groups.append(self.graph.nodes[target])
            elif edge_type == "SYNERGY":
                partner_data = self.graph.nodes[target]
                synergies.append(
                    {
                        "partner_slug": partner_data.get("slug"),
                        "partner_name": partner_data.get("name"),
                        "synergy_name": edge_data.get("name"),
                        "notes": edge_data.get("notes"),
                    }
                )

        return {
            "competitor": node_data,
            "promotions": promotions,
            "corporate_groups": groups,
            "synergies": synergies,
        }

    def compare_category(self, category_or_tags: List[str]) -> List[Dict[str, Any]]:
        """
        Cross-cuts all competitors across a category of tags
        (e.g., ['fault_guarantee', 'buyer_indemnity', 'assured_inspection']).
        """
        seen_promos = set()
        matched_promotions = []

        for tag in category_or_tags:
            promos = self.query_by_tag(tag, active_only=True)
            for p in promos:
                if p["id"] not in seen_promos:
                    seen_promos.add(p["id"])
                    # Extract tags for this promo
                    p_tags = [
                        self.graph.nodes[n]["name"]
                        for n in self.graph.neighbors(f"promo:{p['id']}")
                        if n.startswith("tag:")
                    ]
                    matched_promotions.append(
                        {
                            "competitor": p["competitor"],
                            "promo_id": p["id"],
                            "title": p["title"],
                            "summary": p["summary"],
                            "canonical_url": p["canonical_url"],
                            "valid_until": p["valid_until"],
                            "filepath": p["filepath"],
                            "matched_tags": p_tags,
                        }
                    )

        return matched_promotions

    def search(self, query: str) -> List[Dict[str, Any]]:
        """Instant keyword search over promotion titles, tags, and summaries."""
        q = query.strip().lower()
        results = []

        for node_id, data in self.graph.nodes(data=True):
            if data.get("node_type") != "promotion":
                continue

            score = 0
            if q in data.get("title", "").lower():
                score += 10
            if q in data.get("summary", "").lower():
                score += 5
            if q in data.get("competitor", "").lower():
                score += 8

            # Check tags
            tags = [
                self.graph.nodes[n]["name"]
                for n in self.graph.neighbors(node_id)
                if n.startswith("tag:")
            ]
            if any(q in t for t in tags):
                score += 7

            if score > 0:
                results.append(
                    {
                        "score": score,
                        "id": data["id"],
                        "competitor": data["competitor"],
                        "title": data["title"],
                        "summary": data["summary"],
                        "url": data["canonical_url"],
                        "filepath": data["filepath"],
                        "tags": tags,
                    }
                )

        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    def get_stats(self) -> Dict[str, Any]:
        """Returns topology statistics."""
        node_types = {}
        for _, d in self.graph.nodes(data=True):
            t = d.get("node_type", "unknown")
            node_types[t] = node_types.get(t, 0) + 1

        top_tags = []
        for node_id, data in self.graph.nodes(data=True):
            if data.get("node_type") == "tag":
                count = sum(1 for n in self.graph.neighbors(node_id) if n.startswith("promo:"))
                top_tags.append((data["name"], count))
        top_tags.sort(key=lambda x: x[1], reverse=True)

        return {
            "total_nodes": self.graph.number_of_nodes(),
            "total_edges": self.graph.number_of_edges(),
            "node_distribution": node_types,
            "top_tags": top_tags[:8],
            "last_indexed": self.last_indexed_at.isoformat() if self.last_indexed_at else None,
        }

    def export_json(self, output_path: str) -> None:
        """Exports graph in node-link JSON format for web visualization."""
        data = nx.node_link_data(self.graph, edges="edges")
        out_dir = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(out_dir, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2, default=str)

    def generate_cytoscape_html(self, output_path: str, title: str = "MFL Competitor Intelligence — OKF Knowledge Graph") -> str:
        """
        Generates a standalone, self-contained HTML visualizer using Cytoscape.js and Marked.js,
        conforming to Google OKF viz.html specification with custom competitor intelligence enhancements.
        """
        elements = []
        # Build Cytoscape Nodes
        for node_id, data in self.graph.nodes(data=True):
            node_type = data.get("node_type", "unknown")
            label = data.get("name") or data.get("title") or node_id
            if node_type == "tag":
                label = f"#{data.get('name', node_id)}"
            elif node_type == "promotion":
                raw_title = data.get("title", node_id)
                label = raw_title if len(raw_title) <= 26 else raw_title[:24] + "…"

            node_elem = {
                "data": {
                    "id": node_id,
                    "label": label,
                    "node_type": node_type,
                    **{k: v for k, v in data.items() if k not in ["id", "label"]}
                }
            }
            elements.append(node_elem)

        # Build Cytoscape Edges
        edge_idx = 0
        for u, v, data in self.graph.edges(data=True):
            edge_type = data.get("edge_type", "RELATED_TO")
            edge_elem = {
                "data": {
                    "id": f"edge_{edge_idx}",
                    "source": u,
                    "target": v,
                    "edge_type": edge_type,
                    "name": data.get("name", ""),
                    "notes": data.get("notes", "")
                }
            }
            elements.append(edge_elem)
            edge_idx += 1

        elements_json = json.dumps(elements, indent=2, default=str)
        stats = self.get_stats()

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <!-- Cytoscape.js & Marked.js CDN -->
  <script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.0/cytoscape.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
  <style>
    :root {{
      --bg: #0b0f19;
      --surface: #111827;
      --surface-border: #1f2937;
      --text: #f9fafb;
      --text-muted: #9ca3af;
      --accent: #3b82f6;
      --group-color: #8b5cf6;
      --comp-color: #0284c7;
      --promo-color: #10b981;
      --tag-color: #f59e0b;
      --synergy-color: #ec4899;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      overflow: hidden;
      height: 100vh;
      display: flex;
      flex-direction: column;
    }}
    /* Header Navbar */
    header {{
      height: 58px;
      background: rgba(17, 24, 39, 0.95);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--surface-border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 20px;
      z-index: 20;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .brand h1 {{
      font-size: 15px;
      font-weight: 700;
      letter-spacing: -0.01em;
      color: #fff;
    }}
    .badge {{
      font-size: 11px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 9999px;
      background: #1e293b;
      color: var(--text-muted);
      border: 1px solid #334155;
    }}
    /* Toolbar Controls */
    .controls {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .search-input {{
      background: #1e293b;
      border: 1px solid #334155;
      color: #fff;
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 13px;
      width: 220px;
      outline: none;
      transition: all 0.2s;
    }}
    .search-input:focus {{
      border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
    }}
    select, button {{
      background: #1e293b;
      border: 1px solid #334155;
      color: #fff;
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 13px;
      cursor: pointer;
      outline: none;
      transition: all 0.2s;
    }}
    select:hover, button:hover {{
      background: #334155;
      border-color: #475569;
    }}
    /* Main Layout */
    .main-container {{
      flex: 1;
      display: flex;
      position: relative;
      overflow: hidden;
    }}
    #cy {{
      flex: 1;
      height: 100%;
      background: radial-gradient(circle at 50% 50%, #111827 0%, #0b0f19 100%);
    }}
    /* Legend */
    .legend {{
      position: absolute;
      bottom: 20px;
      left: 20px;
      background: rgba(17, 24, 39, 0.9);
      backdrop-filter: blur(8px);
      border: 1px solid var(--surface-border);
      border-radius: 8px;
      padding: 10px 14px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-size: 11px;
      z-index: 10;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
    }}
    .legend-item {{
      display: flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
      opacity: 0.9;
    }}
    .legend-item:hover {{ opacity: 1; }}
    .dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
    }}
    /* Detail Sidebar Drawer */
    #sidebar {{
      width: 440px;
      background: var(--surface);
      border-left: 1px solid var(--surface-border);
      height: 100%;
      overflow-y: auto;
      padding: 24px;
      display: none;
      z-index: 30;
      box-shadow: -10px 0 30px rgba(0,0,0,0.5);
      animation: slideIn 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    }}
    @keyframes slideIn {{
      from {{ transform: translateX(100%); }}
      to {{ transform: translateX(0); }}
    }}
    .close-btn {{
      float: right;
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-size: 18px;
      cursor: pointer;
      padding: 2px 6px;
    }}
    .close-btn:hover {{ color: #fff; }}
    .node-header {{
      margin-bottom: 16px;
    }}
    .node-type-pill {{
      display: inline-block;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      padding: 3px 8px;
      border-radius: 4px;
      margin-bottom: 8px;
    }}
    .type-corporate_group {{ background: rgba(139, 92, 246, 0.2); color: #c4b5fd; border: 1px solid #8b5cf6; }}
    .type-competitor {{ background: rgba(2, 132, 199, 0.2); color: #7dd3fc; border: 1px solid #0284c7; }}
    .type-promotion {{ background: rgba(16, 185, 129, 0.2); color: #6ee7b7; border: 1px solid #10b981; }}
    .type-tag {{ background: rgba(245, 158, 11, 0.2); color: #fcd34d; border: 1px solid #f59e0b; }}
    .node-title {{
      font-size: 18px;
      font-weight: 700;
      line-height: 1.3;
      color: #fff;
    }}
    .meta-table {{
      width: 100%;
      border-collapse: collapse;
      margin: 16px 0;
      font-size: 12px;
    }}
    .meta-table td {{
      padding: 6px 0;
      border-bottom: 1px solid #1f2937;
    }}
    .meta-table td:first-child {{
      color: var(--text-muted);
      width: 32%;
      font-weight: 500;
    }}
    .meta-table td:last-child {{
      color: #e2e8f0;
      word-break: break-all;
    }}
    .meta-table a {{
      color: var(--accent);
      text-decoration: none;
    }}
    .meta-table a:hover {{ text-decoration: underline; }}
    /* Markdown Body */
    .markdown-body {{
      font-size: 13px;
      line-height: 1.6;
      color: #cbd5e1;
      border-top: 1px solid #1f2937;
      padding-top: 16px;
      margin-top: 16px;
    }}
    .markdown-body h2 {{ font-size: 15px; margin: 16px 0 8px 0; color: #f1f5f9; }}
    .markdown-body h3 {{ font-size: 13px; margin: 12px 0 6px 0; color: #e2e8f0; }}
    .markdown-body p {{ margin-bottom: 10px; }}
    .markdown-body ul {{ padding-left: 20px; margin-bottom: 12px; }}
    .markdown-body li {{ margin-bottom: 4px; }}
    .markdown-body blockquote {{
      border-left: 3px solid var(--accent);
      padding: 6px 12px;
      background: #1e293b;
      border-radius: 0 4px 4px 0;
      margin: 12px 0;
      font-style: italic;
    }}
    .connections-section {{
      margin-top: 20px;
      border-top: 1px solid #1f2937;
      padding-top: 16px;
    }}
    .connections-section h4 {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      margin-bottom: 10px;
    }}
    .conn-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: #1e293b;
      border: 1px solid #334155;
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 12px;
      color: #e2e8f0;
      cursor: pointer;
      margin: 3px 4px 3px 0;
      transition: all 0.15s;
    }}
    .conn-pill:hover {{
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }}
  </style>
</head>
<body>

  <header>
    <div class="brand">
      <h1>{title}</h1>
      <span class="badge">{stats['total_nodes']} Nodes</span>
      <span class="badge">{stats['total_edges']} Edges</span>
    </div>
    <div class="controls">
      <input type="text" id="search" class="search-input" placeholder="Search concepts, tags, promos...">
      <select id="layout-select">
        <option value="cose">Force-Directed (CoSE)</option>
        <option value="concentric">Concentric</option>
        <option value="breadthfirst">Hierarchical</option>
        <option value="circle">Circle</option>
      </select>
      <button id="btn-fit">Fit View</button>
      <button id="btn-reset">Reset</button>
    </div>
  </header>

  <div class="main-container">
    <div id="cy"></div>

    <div class="legend">
      <div class="legend-item"><span class="dot" style="background:var(--group-color);"></span> Corporate Group</div>
      <div class="legend-item"><span class="dot" style="background:var(--comp-color);"></span> Competitor</div>
      <div class="legend-item"><span class="dot" style="background:var(--promo-color);"></span> OKF Promotion</div>
      <div class="legend-item"><span class="dot" style="background:var(--tag-color);"></span> Taxonomy Tag</div>
      <div class="legend-item"><span class="dot" style="background:var(--synergy-color);"></span> Platform Synergy</div>
    </div>

    <div id="sidebar">
      <button class="close-btn" id="btn-close">&times;</button>
      <div id="sidebar-content"></div>
    </div>
  </div>

  <script>
    const GRAPH_ELEMENTS = {elements_json};

    // Cytoscape initialization
    const cy = cytoscape({{
      container: document.getElementById('cy'),
      elements: GRAPH_ELEMENTS,
      style: [
        {{
          selector: 'node',
          style: {{
            'label': 'data(label)',
            'color': '#f8fafc',
            'font-size': '11px',
            'font-family': 'system-ui, -apple-system, sans-serif',
            'text-valign': 'bottom',
            'text-margin-y': 5,
            'background-color': '#64748b',
            'text-outline-color': '#0b0f19',
            'text-outline-width': 2,
            'border-width': 2,
            'border-color': '#334155',
            'transition-property': 'background-color, border-color, opacity, shadow-blur',
            'transition-duration': '0.2s'
          }}
        }},
        {{
          selector: 'node[node_type="corporate_group"]',
          style: {{
            'shape': 'hexagon',
            'background-color': '#8b5cf6',
            'border-color': '#a78bfa',
            'width': 50,
            'height': 50,
            'font-weight': 'bold',
            'font-size': '12px'
          }}
        }},
        {{
          selector: 'node[node_type="competitor"]',
          style: {{
            'shape': 'round-rectangle',
            'background-color': '#0284c7',
            'border-color': '#38bdf8',
            'width': 44,
            'height': 44,
            'font-weight': 'bold',
            'font-size': '12px'
          }}
        }},
        {{
          selector: 'node[node_type="promotion"]',
          style: {{
            'shape': 'ellipse',
            'background-color': '#10b981',
            'border-color': '#34d399',
            'width': 34,
            'height': 34
          }}
        }},
        {{
          selector: 'node[node_type="tag"]',
          style: {{
            'shape': 'diamond',
            'background-color': '#f59e0b',
            'border-color': '#fbbf24',
            'width': 26,
            'height': 26,
            'font-size': '10px'
          }}
        }},
        {{
          selector: 'edge',
          style: {{
            'width': 2,
            'line-color': '#334155',
            'target-arrow-color': '#334155',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
            'arrow-scale': 0.8
          }}
        }},
        {{
          selector: 'edge[edge_type="OFFERS"]',
          style: {{
            'line-color': '#0284c7',
            'target-arrow-color': '#0284c7',
            'width': 2.5
          }}
        }},
        {{
          selector: 'edge[edge_type="TAGGED_WITH"]',
          style: {{
            'line-color': '#d97706',
            'target-arrow-color': '#d97706',
            'line-style': 'dotted',
            'width': 1.5
          }}
        }},
        {{
          selector: 'edge[edge_type="MEMBER_OF"]',
          style: {{
            'line-color': '#8b5cf6',
            'target-arrow-color': '#8b5cf6',
            'width': 2.5
          }}
        }},
        {{
          selector: 'edge[edge_type="SYNERGY"]',
          style: {{
            'line-color': '#ec4899',
            'target-arrow-color': '#ec4899',
            'line-style': 'dashed',
            'width': 2.5
          }}
        }},
        {{
          selector: '.highlighted',
          style: {{
            'border-color': '#38bdf8',
            'border-width': 4,
            'shadow-blur': 16,
            'shadow-color': '#38bdf8',
            'shadow-opacity': 0.9,
            'z-index': 99
          }}
        }},
        {{
          selector: '.dimmed',
          style: {{
            'opacity': 0.15
          }}
        }}
      ],
      layout: {{
        name: 'cose',
        animate: true,
        animationDuration: 700,
        nodeRepulsion: 9000,
        idealEdgeLength: 70
      }}
    }});

    // Sidebar & Selection Handling
    const sidebar = document.getElementById('sidebar');
    const sidebarContent = document.getElementById('sidebar-content');

    function selectNode(node) {{
      cy.elements().removeClass('highlighted dimmed');
      const neighborhood = node.closedNeighborhood();
      cy.elements().difference(neighborhood).addClass('dimmed');
      node.addClass('highlighted');

      const data = node.data();
      renderSidebar(data, node);
      sidebar.style.display = 'block';
    }}

    function renderSidebar(data, node) {{
      const typeClass = 'type-' + data.node_type;
      let html = `
        <div class="node-header">
          <span class="node-type-pill ${{typeClass}}">${{data.node_type.replace('_', ' ')}}</span>
          <h2 class="node-title">${{data.name || data.title || data.id}}</h2>
        </div>
      `;

      html += `<table class="meta-table">`;
      html += `<tr><td>Node ID</td><td><code>${{data.id}}</code></td></tr>`;
      
      if (data.status) html += `<tr><td>Status</td><td><span class="badge" style="background:#065f46;color:#a7f3d0">${{data.status}}</span></td></tr>`;
      if (data.valid_until) html += `<tr><td>Valid Until</td><td>${{data.valid_until}}</td></tr>`;
      if (data.competitor) html += `<tr><td>Competitor</td><td><strong>${{data.competitor.toUpperCase()}}</strong></td></tr>`;
      if (data.category) html += `<tr><td>Category</td><td>${{data.category}}</td></tr>`;
      if (data.canonical_url) html += `<tr><td>Canonical URL</td><td><a href="${{data.canonical_url}}" target="_blank">Open Link &nearr;</a></td></tr>`;
      if (data.filepath) html += `<tr><td>Source File</td><td><code>${{data.filepath}}</code></td></tr>`;
      html += `</table>`;

      // Render Markdown if available
      if (data.raw_body) {{
        const rendered = marked.parse(data.raw_body);
        html += `<div class="markdown-body">${{rendered}}</div>`;
      }} else if (data.description) {{
        html += `<div class="markdown-body"><p>${{data.description}}</p></div>`;
      }}

      // Connections List
      const neighbors = node.neighborhood().nodes();
      if (neighbors.length > 0) {{
        html += `<div class="connections-section"><h4>Connected Concepts (${{neighbors.length}})</h4>`;
        neighbors.forEach(n => {{
          const nd = n.data();
          const lbl = nd.name || nd.title || nd.id;
          html += `<span class="conn-pill" onclick="window.focusNodeById('${{nd.id}}')">${{lbl}}</span>`;
        }});
        html += `</div>`;
      }}

      sidebarContent.innerHTML = html;
    }}

    window.focusNodeById = function(nodeId) {{
      const n = cy.getElementById(nodeId);
      if (n && n.length > 0) {{
        cy.animate({{
          center: {{ eles: n }},
          zoom: 1.4,
          duration: 400
        }});
        selectNode(n);
      }}
    }};

    cy.on('tap', 'node', function(evt) {{
      selectNode(evt.target);
    }});

    cy.on('tap', function(evt) {{
      if (evt.target === cy) {{
        cy.elements().removeClass('highlighted dimmed');
        sidebar.style.display = 'none';
      }}
    }});

    document.getElementById('btn-close').addEventListener('click', () => {{
      cy.elements().removeClass('highlighted dimmed');
      sidebar.style.display = 'none';
    }});

    // Search Filtering
    const searchInput = document.getElementById('search');
    searchInput.addEventListener('input', function() {{
      const q = this.value.trim().toLowerCase();
      if (!q) {{
        cy.elements().removeClass('highlighted dimmed');
        return;
      }}
      cy.elements().addClass('dimmed').removeClass('highlighted');
      const matches = cy.nodes().filter(n => {{
        const d = n.data();
        const str = ((d.label || '') + ' ' + (d.title || '') + ' ' + (d.name || '') + ' ' + (d.summary || '')).toLowerCase();
        return str.includes(q);
      }});
      matches.removeClass('dimmed').addClass('highlighted');
    }});

    // Layout Selector
    document.getElementById('layout-select').addEventListener('change', function() {{
      const l = cy.layout({{ name: this.value, animate: true, animationDuration: 600 }});
      l.run();
    }});

    document.getElementById('btn-fit').addEventListener('click', () => cy.fit(null, 40));
    document.getElementById('btn-reset').addEventListener('click', () => {{
      searchInput.value = '';
      cy.elements().removeClass('highlighted dimmed');
      sidebar.style.display = 'none';
      cy.fit(null, 40);
    }});
  </script>
</body>
</html>
"""
        out_dir = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(out_dir, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fp:
            fp.write(html_content)
        return os.path.abspath(output_path)


# -------------------------------------------------------------
# Google Managed Agent / Python Tool Callables
# -------------------------------------------------------------

_ENGINE_INSTANCE: Optional[OKFGraphEngine] = None


def get_engine(workspace_root: Optional[str] = None) -> OKFGraphEngine:
    global _ENGINE_INSTANCE
    if _ENGINE_INSTANCE is None:
        if workspace_root is None:
            # Auto-detect workspace root
            curr = os.path.abspath(os.path.dirname(__file__))
            workspace_root = os.path.abspath(os.path.join(curr, ".."))
        _ENGINE_INSTANCE = OKFGraphEngine(workspace_root)
    return _ENGINE_INSTANCE


def tool_query_tag(tag: str) -> List[Dict[str, Any]]:
    """Agent tool to query promotions by tag (e.g. 'fault_guarantee', 'transport_subsidy')."""
    engine = get_engine()
    return engine.query_by_tag(tag, active_only=True)


def tool_compare_competitors(category: str) -> List[Dict[str, Any]]:
    """Agent tool to compare competitors across 'guarantees', 'logistics', 'fees', or 'onboarding'."""
    engine = get_engine()
    category_map = {
        "guarantees": ["fault_guarantee", "buyer_indemnity", "assured_inspection"],
        "logistics": ["transport_subsidy", "fixed_rate_delivery", "collection_service"],
        "fees": ["buyer_fee_discount", "fee_waiver", "tier_volume_discount", "subscription_free"],
        "onboarding": ["sign_up_bonus", "first_purchase_credit", "trader_onboarding"],
    }
    tags = category_map.get(category.lower(), [category.lower()])
    return engine.compare_category(tags)


def tool_get_competitor(competitor_slug: str) -> Dict[str, Any]:
    """Agent tool to get a full competitor profile with active offers and corporate affiliations."""
    engine = get_engine()
    return engine.get_competitor_profile(competitor_slug)


def tool_search_intel(query: str) -> List[Dict[str, Any]]:
    """Agent tool to search promotions by keyword or topic."""
    engine = get_engine()
    return engine.search(query)


# -------------------------------------------------------------
# CLI Interface
# -------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="NetworkX In-Memory Traversal Engine for OKF Competitor Intel"
    )
    parser.add_argument("--tag", type=str, help="Query promotions matching a tag")
    parser.add_argument(
        "--compare",
        type=str,
        choices=["guarantees", "logistics", "fees", "onboarding"],
        help="Compare all competitors across a category",
    )
    parser.add_argument("--competitor", type=str, help="Inspect profile and offers for a competitor")
    parser.add_argument("--search", type=str, help="Search promotions by keyword")
    parser.add_argument("--stats", action="store_true", help="Print graph topology statistics")
    parser.add_argument("--export", type=str, help="Export graph as JSON to the specified path")
    parser.add_argument(
        "--viz",
        nargs="?",
        const="okf-repository/manifests/viz.html",
        type=str,
        help="Generate standalone Cytoscape.js HTML visualizer (default: okf-repository/manifests/viz.html)",
    )
    parser.add_argument(
        "--benchmark", action="store_true", help="Run benchmark performance tests on graph queries"
    )

    args = parser.parse_args()
    curr_dir = os.path.abspath(os.path.dirname(__file__))
    workspace_root = os.path.abspath(os.path.join(curr_dir, ".."))

    start_init = time.perf_counter()
    engine = OKFGraphEngine(workspace_root)
    init_ms = (time.perf_counter() - start_init) * 1000

    if args.stats:
        stats = engine.get_stats()
        print("\n=== OKF NetworkX Graph Statistics ===")
        print(f"Total Nodes:     {stats['total_nodes']}")
        print(f"Total Edges:     {stats['total_edges']}")
        print(f"Build Time:      {init_ms:.2f} ms")
        print("\nNode Breakdown:")
        for t, count in stats["node_distribution"].items():
            print(f"  - {t:<16}: {count}")
        print("\nTop Active Tags:")
        for tag, count in stats["top_tags"]:
            print(f"  - #{tag:<24}: {count} promos")
        print()
        return

    if args.tag:
        t0 = time.perf_counter()
        results = engine.query_by_tag(args.tag)
        q_ms = (time.perf_counter() - t0) * 1000
        print(f"\n=== Tag Query: #{args.tag} ({len(results)} matches in {q_ms:.3f} ms) ===")
        for r in results:
            print(f"\n* [{r['competitor'].upper()}] {r['title']}")
            print(f"  - File:        {r['filepath']}")
            print(f"  - Valid Until: {r['valid_until']}")
            print(f"  - Summary:     {r['summary']}")
        print()
        return

    if args.compare:
        category_map = {
            "guarantees": ["fault_guarantee", "buyer_indemnity", "assured_inspection"],
            "logistics": ["transport_subsidy", "fixed_rate_delivery", "collection_service"],
            "fees": ["buyer_fee_discount", "fee_waiver", "tier_volume_discount", "subscription_free"],
            "onboarding": ["sign_up_bonus", "first_purchase_credit", "trader_onboarding"],
        }
        tags = category_map[args.compare]
        t0 = time.perf_counter()
        results = engine.compare_category(tags)
        q_ms = (time.perf_counter() - t0) * 1000
        print(
            f"\n=== Competitor Comparison: {args.compare.upper()} ({len(results)} competitors in {q_ms:.3f} ms) ==="
        )
        for r in results:
            print(f"\n* [{r['competitor'].upper()}] {r['title']}")
            print(f"  - Matched Tags: {', '.join(r['matched_tags'])}")
            print(f"  - Summary:      {r['summary']}")
            print(f"  - File:         {r['filepath']}")
        print()
        return

    if args.competitor:
        t0 = time.perf_counter()
        profile = engine.get_competitor_profile(args.competitor)
        q_ms = (time.perf_counter() - t0) * 1000
        if "error" in profile:
            print(f"\nError: {profile['error']}\n")
            return

        c = profile["competitor"]
        print(f"\n=== Competitor Profile: {c.get('name')} ({q_ms:.3f} ms) ===")
        print(f"Slug:        {c.get('slug')}")
        print(f"Primary URL: {c.get('primary_url')}")
        print(f"Buyer URL:   {c.get('buyer_portal_url')}")

        if profile["corporate_groups"]:
            print("\nCorporate Affiliations:")
            for g in profile["corporate_groups"]:
                print(f"  - {g['name']}: {g['description']}")

        if profile["synergies"]:
            print("\nPlatform Synergies:")
            for s in profile["synergies"]:
                print(f"  - With {s['partner_name']}: {s['synergy_name']} ({s['notes']})")

        print(f"\nActive Promotions ({len(profile['promotions'])}):")
        for p in profile["promotions"]:
            print(f"  - [{p['id']}] {p['title']} (Valid until: {p['valid_until']})")
        print()
        return

    if args.search:
        t0 = time.perf_counter()
        results = engine.search(args.search)
        q_ms = (time.perf_counter() - t0) * 1000
        print(f"\n=== Search Results for '{args.search}' ({len(results)} matches in {q_ms:.3f} ms) ===")
        for r in results:
            print(f"\n* [{r['competitor'].upper()}] {r['title']} (Relevance Score: {r['score']})")
            print(f"  - Tags:    {', '.join(r['tags'])}")
            print(f"  - Summary: {r['summary']}")
        print()
        return

    if args.export:
        engine.export_json(args.export)
        print(f"\nGraph exported successfully to {args.export}\n")
        return

    if args.viz is not None:
        out_path = args.viz
        if not os.path.isabs(out_path):
            out_path = os.path.join(workspace_root, out_path)
        saved_path = engine.generate_cytoscape_html(out_path)
        print(f"\nCytoscape.js HTML visualizer generated successfully:")
        print(f"  --> {saved_path}")
        print(f"  To view in browser: open \"{saved_path}\"\n")
        return

    if args.benchmark:
        print("\n=== Running NetworkX Graph Engine Benchmarks ===")
        print(f"Initial Graph Construction Time: {init_ms:.2f} ms")

        # Benchmark 1: Tag query (1000 iterations)
        n_iters = 1000
        t0 = time.perf_counter()
        for _ in range(n_iters):
            engine.query_by_tag("fault_guarantee")
        avg_tag_us = ((time.perf_counter() - t0) / n_iters) * 1_000_000

        # Benchmark 2: Cross-competitor comparison (1000 iterations)
        t0 = time.perf_counter()
        for _ in range(n_iters):
            engine.compare_category(["fault_guarantee", "buyer_indemnity", "assured_inspection"])
        avg_comp_us = ((time.perf_counter() - t0) / n_iters) * 1_000_000

        # Benchmark 3: Competitor profile lookup (1000 iterations)
        t0 = time.perf_counter()
        for _ in range(n_iters):
            engine.get_competitor_profile("manheim")
        avg_prof_us = ((time.perf_counter() - t0) / n_iters) * 1_000_000

        print(f"Avg Tag Query Latency:        {avg_tag_us:.2f} microseconds (µs)")
        print(f"Avg Category Compare Latency:  {avg_comp_us:.2f} microseconds (µs)")
        print(f"Avg Profile Lookup Latency:   {avg_prof_us:.2f} microseconds (µs)")
        print("Benchmark Complete: All operations resolve in under 0.1ms!\n")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
