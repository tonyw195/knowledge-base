#!/usr/bin/env python3
"""
cf_browser.py — Cloudflare Browser Rendering Client

Renders JavaScript-heavy pages, SPAs, and social media feeds (e.g. X, LinkedIn, Carwow, Motorway)
via Cloudflare's Browser Run service, returning structured Markdown directly.
"""

import argparse
import sys
import os

try:
    from scripts import env_vars
except ImportError:
    # Allow running directly from scripts/ directory
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    import env_vars


def render_url_to_markdown(
    url: str,
    viewport_width: int = 1280,
    viewport_height: int = 800,
) -> str:
    """Invokes Cloudflare Browser Rendering to capture and convert a web page to Markdown."""
    api_token = env_vars.CLOUDFLARE_API_TOKEN
    account_id = env_vars.CLOUDFLARE_ACCOUNT_ID

    if not api_token or not account_id:
        print(
            "[ERROR] Missing CLOUDFLARE_API_TOKEN or CLOUDFLARE_ACCOUNT_ID in environment (.env).",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        import cloudflare
    except ImportError:
        print(
            "[ERROR] 'cloudflare' SDK not installed. Run 'pip install cloudflare'.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = cloudflare.Cloudflare(api_token=api_token)

    try:
        result = client.browser_rendering.markdown.create(
            account_id=account_id,
            url=url,
            viewport={"width": viewport_width, "height": viewport_height},
        )
        # Handle string return or object with text/markdown/content attribute
        if isinstance(result, str):
            return result
        elif hasattr(result, "markdown"):
            return result.markdown
        elif hasattr(result, "text"):
            return result.text
        return str(result)
    except Exception as e:
        print(f"[ERROR] Cloudflare Browser Rendering failed for URL '{url}': {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Render dynamic webpages, SPAs, and social media to Markdown using Cloudflare Browser Run."
    )
    parser.add_argument("url", nargs="?", help="Target URL to render to Markdown")
    parser.add_argument("--url", dest="opt_url", help="Target URL (option flag)")
    parser.add_argument("--output", "-o", help="Optional file path to save rendered Markdown output")
    parser.add_argument("--width", type=int, default=1280, help="Viewport width in pixels (default: 1280)")
    parser.add_argument("--height", type=int, default=800, help="Viewport height in pixels (default: 800)")
    args = parser.parse_args()

    target_url = args.url or args.opt_url
    if not target_url:
        parser.print_help()
        sys.exit(1)

    markdown_content = render_url_to_markdown(
        url=target_url,
        viewport_width=args.width,
        viewport_height=args.height,
    )

    if args.output:
        out_path = os.path.abspath(args.output)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fp:
            fp.write(markdown_content)
        print(f"Successfully rendered '{target_url}' to {out_path}")
    else:
        print(markdown_content)


if __name__ == "__main__":
    main()
