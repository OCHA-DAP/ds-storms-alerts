"""Generate the historical example alerts published on GitHub Pages.

For each showcase advisory this renders BOTH layouts from live dev-DB data:

- ``<slug>.html`` — the condensed email, wrapped in the real Listmonk campaign
  template (what a subscriber's mail client shows, minus the xlsx attachment);
- ``<slug>-full.html`` — the full-detail layout (deterministic + probabilistic
  maps and every per-threshold strip chart), which is what the email's
  "full charts online" pointer means.

plus ``index.html``, the browser page linking them all. Everything lands in
``docs/alerts/`` and ships with GitHub Pages (main branch, /docs), so a plain
commit publishes it.

The point is layout feedback: five storms spanning a Cat-5 multi-country hit
(Beryl, Melissa) down to a two-island brush (Jerry), so reviewers can judge the
format against both the busiest and the quietest email it will ever produce.

Run manually after layout changes:

    uv run python pipelines/generate_showcase.py

Requires DSCI_LISTMONK_* env vars for the email chrome (falls back to a plain
wrapper if Listmonk is unreachable) and dev-DB access via ocha-stratus.

DB and blob results are cached on disk in ``.showcase-cache/`` (the archived
advisories never change; only the plots do), so re-runs after a layout change
skip every fetch. ``--refresh`` clears the cache first; ``--no-cache``
bypasses it entirely. Basemap tiles cache alongside via contextily.
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import contextily as ctx
import ocha_stratus as stratus

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipelines.run_alert import _build_subject, generate_alert_html  # noqa: E402
from src.preview import PreviewUnavailable, render_with_template  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent.parent / ".showcase-cache"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DOCS_ALERTS = Path(__file__).resolve().parent.parent / "docs" / "alerts"

# One entry per example. `blurb` is the browser-page description — why this
# storm is in the set. The advisory chosen is the peak-exposure one (checked in
# the dev DB), except where noted.
SHOWCASE = [
    {
        "slug": "beryl-2024",
        "issued": "2024-07-02T18",
        "title": "Hurricane Beryl — July 2024",
        "blurb": (
            "Category-5 crossing of the eastern Caribbean: seven countries in "
            "the forecast swath at once, including Jamaica ahead of a direct hit."
        ),
    },
    {
        "slug": "melissa-2025",
        "issued": "2025-10-24T18",
        "title": "Hurricane Melissa — October 2025",
        "blurb": (
            "The busiest alert in the set: 12M+ exposed at peak across the "
            "Greater Antilles, with Jamaica facing a rare direct hurricane hit."
        ),
    },
    {
        "slug": "oscar-2024",
        "issued": "2024-10-20T06",
        "title": "Hurricane Oscar — October 2024",
        "blurb": (
            "A small, late-forming hurricane: moderate exposure concentrated "
            "in eastern Cuba and the Bahamas."
        ),
    },
    {
        "slug": "erin-2025",
        "issued": "2025-08-16T06",
        "title": "Hurricane Erin — August 2025",
        "blurb": (
            "A low-exposure story: a large hurricane staying offshore, "
            "brushing small territories (Anguilla, the Virgin Islands, Bermuda)."
        ),
    },
    {
        "slug": "jerry-2025",
        "issued": "2025-10-09T12",
        "title": "Tropical Storm Jerry — October 2025",
        "blurb": (
            "The quietest alert format: a Lesser-Antilles brush with tens of "
            "thousands exposed on two small islands."
        ),
    },
]

_NAV = (
    '<nav style="background:#007eb5;padding:11px 20px;display:flex;'
    'align-items:center;gap:24px;font-family:system-ui,sans-serif;'
    'font-size:0.95em;flex-wrap:wrap">'
    '<span style="color:#fff;font-weight:700;margin-right:auto">Storm Alerts'
    "</span>"
    '<a href="../index.html" style="color:#fff;text-decoration:none;'
    'opacity:0.85">Subscribe</a>'
    '<a href="../guide.html" style="color:#fff;text-decoration:none;'
    'opacity:0.85">About the alerts</a>'
    '<a href="index.html" style="color:#fff;text-decoration:none;'
    'font-weight:700;border-bottom:2px solid #fff;padding-bottom:1px">'
    "Example alerts</a></nav>"
)


def _banner(entry: dict, variant: str) -> str:
    """The strip above each example page: what it is + the layout switcher."""
    other = (
        f'<a href="{entry["slug"]}-full.html" style="color:#007eb5">'
        "switch to the full-detail version</a>"
        if variant == "email"
        else f'<a href="{entry["slug"]}.html" style="color:#007eb5">'
        "switch to the email version</a>"
    )
    label = (
        "Condensed email layout (the real email also carries a spreadsheet "
        "attachment with all admin-1 figures)."
        if variant == "email"
        else "Full-detail layout — everything the condensed email links out to."
    )
    return (
        '<div style="background:#fff8e1;border-bottom:1px solid #e0d5a8;'
        "padding:10px 20px;font-family:system-ui,sans-serif;font-size:0.9em;"
        'color:#555">'
        f"<strong>Example alert</strong> — {html.escape(entry['title'])}, "
        f"advisory {entry['issued']} UTC. {label} {other} &middot; "
        '<a href="index.html" style="color:#007eb5">all examples</a>'
        "</div>"
    )


def _inject_after_body(page: str, snippet: str) -> str:
    m = re.search(r"<body[^>]*>", page)
    if not m:
        return snippet + page
    return page[: m.end()] + snippet + page[m.end():]


def _plain_wrap(body: str, title: str) -> str:
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title></head>"
        "<body style='margin:0;padding:0;background:#f6f6f6'>"
        "<div style='max-width:940px;margin:0 auto;padding:24px;"
        "background:#fff;font-family:sans-serif'>"
        f"{body}</div></body></html>"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true",
                    help="clear the data cache and refetch everything")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass the data cache without touching it")
    ap.add_argument("--only", metavar="SLUG[,SLUG]",
                    help="regenerate only these examples (skips index.html — "
                         "with data cached, rendering is the slow part, and "
                         "one storm beats five while iterating on layout)")
    args = ap.parse_args()

    showcase = SHOWCASE
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        unknown = wanted - {e["slug"] for e in SHOWCASE}
        if unknown:
            ap.error(f"unknown slug(s): {sorted(unknown)} "
                     f"(valid: {[e['slug'] for e in SHOWCASE]})")
        showcase = [e for e in SHOWCASE if e["slug"] in wanted]

    if args.refresh and CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
        logger.info(f"Cleared data cache {CACHE_DIR}")
    if args.no_cache:
        os.environ.pop("STORMS_ALERTS_DATA_CACHE", None)
    else:
        os.environ.setdefault("STORMS_ALERTS_DATA_CACHE", str(CACHE_DIR))
        ctx.set_cache_dir(str(CACHE_DIR / "tiles"))
        logger.info(f"Data cache: {os.environ['STORMS_ALERTS_DATA_CACHE']}")

    DOCS_ALERTS.mkdir(parents=True, exist_ok=True)
    engine = stratus.get_engine(stage="dev")
    rows: list[dict] = []

    for entry in showcase:
        t = datetime.strptime(entry["issued"], "%Y-%m-%dT%H")
        logger.info(f"=== {entry['slug']} ({entry['issued']})")

        logger.info("  condensed email layout...")
        result = generate_alert_html(engine, t, full=False)
        if result is None:
            logger.warning(f"  no exposure at {entry['issued']} — skipping")
            continue
        body_email, iso3s, names = result
        subject = _build_subject(t, names)
        try:
            page_email = render_with_template(
                body_email, subject,
                f"ds-storms-alerts_{entry['issued']}_showcase",
            )
        except PreviewUnavailable as exc:
            logger.warning(f"  Listmonk unavailable ({exc}); plain wrapper")
            page_email = _plain_wrap(body_email, subject)
        page_email = _inject_after_body(page_email, _NAV + _banner(entry, "email"))
        (DOCS_ALERTS / f"{entry['slug']}.html").write_text(
            page_email, encoding="utf-8")

        logger.info("  full-detail layout...")
        body_full, _, _ = generate_alert_html(engine, t, full=True)
        page_full = _inject_after_body(
            _plain_wrap(body_full, f"{subject} — full detail"),
            _NAV + _banner(entry, "full"),
        )
        (DOCS_ALERTS / f"{entry['slug']}-full.html").write_text(
            page_full, encoding="utf-8")

        rows.append({
            **entry,
            "storms": ", ".join(names),
            "n_countries": len(iso3s),
        })
        logger.info(f"  done: storms={names} countries={len(iso3s)}")

    # ---- browser page ----------------------------------------------------
    if args.only:
        logger.info(f"Wrote {len(rows)} example(s); index.html untouched (--only)")
        return
    cards = "".join(
        f"""
  <div style="background:#fff;border:1px solid #e2e2e2;border-radius:8px;
              padding:18px 22px;margin:0 0 14px">
    <div style="font-size:1.1em;font-weight:600;margin-bottom:2px">
      {html.escape(r["title"])}</div>
    <div style="font-size:0.85em;color:#888;margin-bottom:8px">
      Advisory {r["issued"]} UTC &middot; storms in this email:
      {html.escape(r["storms"])} &middot; {r["n_countries"]}
      countr{"y" if r["n_countries"] == 1 else "ies"}</div>
    <p style="margin:0 0 12px;color:#444;font-size:0.95em;line-height:1.5">
      {html.escape(r["blurb"])}</p>
    <a href="{r["slug"]}.html" style="display:inline-block;background:#007eb5;
       color:#fff;text-decoration:none;padding:7px 14px;border-radius:4px;
       font-size:0.9em;font-weight:600">Email version</a>
    <a href="{r["slug"]}-full.html" style="display:inline-block;color:#007eb5;
       text-decoration:none;padding:7px 14px;font-size:0.9em">
       Full detail version</a>
  </div>"""
        for r in rows
    )
    index = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Example storm alerts</title>
</head>
<body style="margin:0;background:#f6f6f6;font-family:system-ui,sans-serif;color:#222">
{_NAV}
<div style="max-width:760px;margin:0 auto;padding:28px 20px">
  <h1 style="font-size:1.5em;color:#007eb5;margin:0 0 8px">Example alerts</h1>
  <p style="color:#555;line-height:1.6;margin:0 0 6px">
    Five historical advisories re-rendered in the current alert layout — from
    the busiest email the system produces down to the quietest. Each comes in
    two versions: the <strong>condensed email</strong> subscribers receive, and
    the <strong>full-detail</strong> page it links to (all maps and
    per-threshold exposure charts).</p>
  <p style="color:#555;line-height:1.6;margin:0 0 22px">
    These pages exist to collect feedback on the format —
    <a href="https://github.com/OCHA-DAP/ds-storms-alerts/issues"
       style="color:#007eb5">open an issue</a> or reply to any alert email.
    Figures are re-generated from the archived forecasts and may differ
    slightly from what was sent at the time.</p>
{cards}
</div>
</body>
</html>
"""
    (DOCS_ALERTS / "index.html").write_text(index, encoding="utf-8")
    logger.info(f"Wrote {len(rows)} examples + index to {DOCS_ALERTS}")


if __name__ == "__main__":
    main()
