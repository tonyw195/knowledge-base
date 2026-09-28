#!/usr/bin/env python3
"""
precheck_dedup.py — Deduplication, Freshness, and Tag Query Engine for MFL Intel Swarm

Purpose:
  1. Deduplication: Pre-check if an active (unexpired) promotion matching a target URL
     or content hash exists in okf-repository/competitors/.
  2. Tag Discovery: Query promotions across competitors matching specific keyword tags.

Exit Codes:
  1 = Match Found & Active -> SKIP SEARCH / SCRAPE
  0 = No Match, Expired, or Tag query completed -> PROCEED WITH SEARCH / SCRAPE
"""

import argparse
import datetime
import os
import re
import sys
from urllib.parse import urlparse, urlunparse

def normalize_url(url: str) -> str:
    """Normalize URL by stripping trailing slashes, fragments, and standardizing scheme/host."""
    if not url:
        return ""
    url = url.strip()
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip('/')
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ''))

def parse_simple_yaml_frontmatter(content: str) -> dict:
    """
    Zero-dependency parser for OKF v0.2 YAML front matter enclosed in '---'.
    Extracts scalar fields and list fields (like tags) without external dependencies.
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL)
    if not match:
        return {}
    
    yaml_text = match.group(1)
    data = {"tags": []}
    current_list_key = None
    
    for line in yaml_text.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith('#'):
            continue
        
        # Check if this line is an item in a list (e.g. "  - transport_subsidy")
        if trimmed.startswith('- ') and current_list_key:
            item_val = trimmed[2:].strip().strip('"\'')
            if current_list_key in data and isinstance(data[current_list_key], list):
                data[current_list_key].append(item_val)
            continue
        
        # Check key: value
        if ':' in trimmed:
            parts = trimmed.split(':', 1)
            key = parts[0].strip()
            raw_val = parts[1].strip()
            
            # Handle inline flow array: e.g. tags: [tag-1, tag-2] or ["tag-1", "tag-2"]
            if raw_val.startswith('[') and raw_val.endswith(']'):
                current_list_key = None
                inner = raw_val[1:-1].strip()
                if inner:
                    items = [x.strip().strip('"\'') for x in inner.split(',') if x.strip()]
                    data[key] = items
                else:
                    data[key] = []
            elif raw_val == "":
                # Start of a multi-line list (e.g. "tags:")
                current_list_key = key
                if key not in data:
                    data[key] = []
            else:
                current_list_key = None
                data[key] = raw_val.strip('"\'')
            
    return data

def parse_date(date_str: str) -> datetime.datetime:
    """Parses date strings like YYYY-MM-DD or ISO 8601 strings into timezone-aware datetime."""
    if not date_str:
        return None
    date_str = str(date_str).strip().strip('"\'')
    
    # Try ISO 8601 with Z or offset
    try:
        if date_str.endswith('Z'):
            date_str = date_str[:-1] + '+00:00'
        return datetime.datetime.fromisoformat(date_str)
    except ValueError:
        pass
    
    # Try YYYY-MM-DD
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        return dt.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        pass

    return None

def is_promotion_active(meta: dict, now: datetime.datetime) -> bool:
    """Checks whether the promotion is considered active and unexpired."""
    status = meta.get("status", "").lower()
    if status in ["expired", "superseded", "inactive"]:
        return False

    expiry_dt = None
    if "stale_after" in meta:
        expiry_dt = parse_date(meta["stale_after"])
    elif "valid_until" in meta:
        expiry_dt = parse_date(meta["valid_until"])

    if expiry_dt is not None:
        if now > expiry_dt:
            return False

    return True

def query_by_tag(repo_dir: str, target_tag: str):
    """Searches and prints all promotions containing a specific keyword tag."""
    now = datetime.datetime.now(datetime.timezone.utc)
    target_tag = target_tag.strip().lower()
    matches = []

    for root, _, files in os.walk(repo_dir):
        for f in files:
            if not f.endswith(".md"):
                continue
            filepath = os.path.join(root, f)
            try:
                with open(filepath, "r", encoding="utf-8") as fp:
                    content = fp.read()
            except Exception:
                continue

            meta = parse_simple_yaml_frontmatter(content)
            tags = [t.lower() for t in meta.get("tags", [])]
            if target_tag in tags:
                matches.append({
                    "path": filepath,
                    "id": meta.get("id", "unknown"),
                    "competitor": meta.get("competitor", "unknown"),
                    "status": meta.get("status", "unknown"),
                    "active": is_promotion_active(meta, now),
                    "tags": meta.get("tags", []),
                    "valid_until": meta.get("valid_until", "N/A"),
                    "url": meta.get("canonical_url", "N/A")
                })

    print(f"\n=== Intel Tag Query: #{target_tag} (Matches: {len(matches)}) ===")
    if not matches:
        print("No promotions found with this tag.")
        return 0

    for m in matches:
        status_label = "ACTIVE" if m["active"] else f"EXPIRED ({m['status']})"
        print(f"\n* [{m['competitor'].upper()}] {m['id']} [{status_label}]")
        print(f"  - Canonical URL: {m['url']}")
        print(f"  - Valid Until:   {m['valid_until']}")
        print(f"  - File Path:     {m['path']}")
        print(f"  - All Tags:      {', '.join(m['tags'])}")
    print()
    return 0

def scan_repository(repo_dir: str, target_url: str = None, target_hash: str = None):
    now = datetime.datetime.now(datetime.timezone.utc)
    norm_target_url = normalize_url(target_url) if target_url else None
    norm_target_hash = target_hash.strip().lower() if target_hash else None

    if not os.path.exists(repo_dir):
        print(f"[PRECHECK] Repo directory '{repo_dir}' does not exist.")
        return 0, None

    matching_files = []

    for root, _, files in os.walk(repo_dir):
        for f in files:
            if not f.endswith(".md"):
                continue
            filepath = os.path.join(root, f)
            try:
                with open(filepath, "r", encoding="utf-8") as fp:
                    content = fp.read()
            except Exception:
                continue

            meta = parse_simple_yaml_frontmatter(content)
            if not meta:
                continue

            url_match = False
            hash_match = False

            if norm_target_url:
                canonical = normalize_url(meta.get("canonical_url", ""))
                if canonical and canonical == norm_target_url:
                    url_match = True

            if norm_target_hash:
                doc_hash = str(meta.get("content_hash", "")).strip().lower()
                if doc_hash and doc_hash == norm_target_hash:
                    hash_match = True

            if url_match or hash_match:
                active = is_promotion_active(meta, now)
                matching_files.append({
                    "path": filepath,
                    "id": meta.get("id", "unknown"),
                    "competitor": meta.get("competitor", "unknown"),
                    "valid_until": meta.get("valid_until", "N/A"),
                    "stale_after": meta.get("stale_after", "N/A"),
                    "status": meta.get("status", "unknown"),
                    "tags": meta.get("tags", []),
                    "active": active
                })

    if not matching_files:
        print("[PRECHECK: 0] No matching promotion found. OK to search.")
        return 0, None

    # Check if any matching file is active
    active_matches = [m for m in matching_files if m["active"]]
    if active_matches:
        match = active_matches[0]
        print(f"[PRECHECK: 1] Match Found & Active! SKIP SEARCH.")
        print(f"  - File: {match['path']}")
        print(f"  - Promo ID: {match['id']}")
        print(f"  - Competitor: {match['competitor']}")
        print(f"  - Status: {match['status']}")
        print(f"  - Valid Until: {match['valid_until']}")
        if match["tags"]:
            print(f"  - Tags: {', '.join(match['tags'])}")
        return 1, match
    else:
        print(f"[PRECHECK: 0] Match found but EXPIRED/INACTIVE. OK to search.")
        for m in matching_files:
            print(f"  - Expired promo: {m['id']} ({m['path']})")
        return 0, None

def main():
    parser = argparse.ArgumentParser(description="MFL Swarm Deduplication & Tag Query Engine")
    parser.add_argument("--url", type=str, help="Target URL to check for existing active promotions")
    parser.add_argument("--hash", type=str, help="MD5 or content hash of promotional offer")
    parser.add_argument("--tag", type=str, help="Query all promotions across competitors by keyword tag")
    parser.add_argument("--dir", type=str, default="okf-repository/competitors", help="Directory containing OKF files")
    
    args = parser.parse_args()

    if args.tag:
        sys.exit(query_by_tag(repo_dir=args.dir, target_tag=args.tag))

    if not args.url and not args.hash:
        print("[ERROR] Must provide either --url, --hash, or --tag argument.")
        sys.exit(2)

    code, _ = scan_repository(repo_dir=args.dir, target_url=args.url, target_hash=args.hash)
    sys.exit(code)

if __name__ == "__main__":
    main()
