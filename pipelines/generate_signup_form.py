"""Generate the sign-up pages for GH Pages: one form, countries grouped by basin.

Queries Listmonk for every list tagged 'ds-storms-alerts' and writes two
self-contained pages from the same basin configuration (BASINS below):

  docs/index.html          the public page
  docs/staging/index.html  an unlisted copy (noindex, linked from nowhere)
                           where a basin in testing can already be signed up
                           for, before it opens on the public page

Every basin is listed on both pages. A basin's status decides what each page
shows: "live" gets its country checkboxes, "development" a short note. To open
a basin publicly, set its "public" status to "live" and regenerate.

Usage:
    uv run python pipelines/generate_signup_form.py
"""

import html
from pathlib import Path

from ocha_relay.listmonk import ListmonkClient

from src.constants import COUNTRY_LIST_TAG

# Status per page: "live" (checkboxes) or "development" (a note).
BASINS: list[dict] = [
    {
        "key": "nhc",
        "title": "North Atlantic and eastern Pacific",
        "source": "Forecasts from the US National Hurricane Center (NHC)",
        "status": {"public": "live", "staging": "live"},
        "bundle": {
            "tag": "aggregate:lac",
            "label": "All of Latin America and the Caribbean",
            "note": "Every alert that includes at least one Caribbean, Central "
                    "American or South American country, or Mexico.",
            "checked": True,
        },
        "regions": [
            ("Caribbean", [
                "ABW", "AIA", "ATG", "BES", "BHS", "BLM", "BMU", "BRB",
                "CUB", "CUW", "CYM", "DMA", "DOM", "GLP", "GRD", "HTI",
                "JAM", "KNA", "LCA", "MAF", "MSR", "MTQ", "PRI", "SPM",
                "SXM", "TCA", "TTO", "VCT", "VGB", "VIR",
            ]),
            ("Central America", ["BLZ", "CRI", "GTM", "HND", "MEX", "NIC", "PAN", "SLV"]),
            ("South America", ["COL", "GUF", "GUY", "SUR", "VEN"]),
            ("North America", ["CAN", "USA"]),
            ("Africa and Atlantic islands", ["CPV", "DZA", "ESH", "GMB", "MAR", "MRT", "SEN"]),
            ("Europe", [
                "BEL", "DEU", "DNK", "ESP", "FRA", "FRO", "GBR", "GGY",
                "GIB", "GRL", "IMN", "IRL", "ISL", "JEY", "LUX", "NLD", "NOR", "PRT",
            ]),
        ],
    },
    {
        "key": "sp",
        "title": "South Pacific",
        "source": "Forecasts from RSMC Nadi (Fiji Meteorological Service). "
                  "Winds are 10-minute means.",
        "status": {"public": "development", "staging": "live"},
        "development_note": "Alerts are being tested; sign-up will open here soon.",
        "bundle": {
            "tag": "aggregate:sp",
            "label": "All South Pacific countries",
            "note": "Every South Pacific alert, whichever countries it includes.",
            "checked": False,
        },
        # Kiribati is left out until there is population data for it:
        # WorldPop 2026 has none, so its alerts could never fire.
        "regions": [
            ("Melanesia", ["FJI", "NCL", "SLB", "VUT"]),
            ("Polynesia", ["ASM", "COK", "NIU", "PCN", "PYF", "TKL", "TON", "TUV", "WLF", "WSM"]),
            ("Micronesia", ["NRU"]),
        ],
    },
    {
        "key": "si",
        "title": "South-West Indian Ocean",
        "source": "RSMC La Réunion",
        "status": {"public": "development", "staging": "development"},
        "development_note": "The next basin to be added.",
    },
    {
        "key": "au",
        "title": "Australian region",
        "source": "Australian Bureau of Meteorology",
        "status": {"public": "development", "staging": "development"},
        "development_note": "Not operational yet.",
    },
    {
        "key": "wp",
        "title": "Western North Pacific",
        "source": "RSMC Tokyo (Japan Meteorological Agency)",
        "status": {"public": "development", "staging": "development"},
        "development_note": "Not operational yet.",
    },
    {
        "key": "ni",
        "title": "North Indian Ocean",
        "source": "RSMC New Delhi (India Meteorological Department)",
        "status": {"public": "development", "staging": "development"},
        "development_note": "Not operational yet.",
    },
]

# Country list names are "Storm Alerts - <country>"; the form shows <country>.
_PREFIX = "Storm Alerts - "


def _short(name: str) -> str:
    return name[len(_PREFIX):] if name.startswith(_PREFIX) else name


def _checkbox(lst: dict, checked: bool = False, label: str | None = None) -> str:
    chk = " checked" if checked else ""
    text = label if label is not None else _short(lst["name"])
    return (
        f'<label class="cb"><input type="checkbox" name="l" value="{lst["uuid"]}"{chk}>'
        f'<span>{html.escape(text)}</span></label>'
    )


def _basin_section(basin: dict, page: str, by_iso3: dict, aggregates: dict) -> str:
    status = basin["status"][page]
    title = html.escape(basin["title"])
    source = html.escape(basin["source"])
    if status != "live":
        return f"""
  <section class="basin dev" id="basin-{basin['key']}">
    <header><h2>{title}</h2><span class="pill dev">In development</span></header>
    <p class="source">{source}</p>
    <p class="devnote">{html.escape(basin.get('development_note', 'Not operational yet.'))}</p>
  </section>"""

    parts = []
    bundle = basin.get("bundle")
    if bundle and bundle["tag"] in aggregates:
        parts.append(
            f'<div class="bundle">{_checkbox(aggregates[bundle["tag"]], bundle["checked"], bundle["label"])}'
            f'<p>{html.escape(bundle["note"])}</p></div>'
        )
    for region, iso3s in basin["regions"]:
        items = [by_iso3[c] for c in iso3s if c in by_iso3]
        if not items:
            continue
        items.sort(key=lambda lst: _short(lst["name"]))
        rid = f"{basin['key']}-{region.lower().replace(' ', '-')}"
        boxes = "".join(_checkbox(lst) for lst in items)
        parts.append(f"""
    <fieldset>
      <legend>{html.escape(region)}
        <button type="button" class="tog" data-region="{rid}" data-state="0">Select all</button>
      </legend>
      <div class="cb-grid" id="region-{rid}">{boxes}</div>
    </fieldset>""")
    if not parts:
        # Live in the config but no lists in Listmonk yet.
        parts.append('<p class="devnote">Country lists are not set up yet.</p>')
    return f"""
  <section class="basin" id="basin-{basin['key']}">
    <header><h2>{title}</h2><span class="pill live">Live</span></header>
    <p class="source">{source}</p>
    {''.join(parts)}
  </section>"""


def generate(listmonk_url: str, all_lists: list[dict], page: str = "public") -> str:
    by_iso3: dict[str, dict] = {}
    aggregates: dict[str, dict] = {}
    for lst in all_lists:
        for tag in lst.get("tags", []):
            if tag.startswith("iso3:"):
                by_iso3[tag[5:]] = lst
            elif tag.startswith("aggregate:"):
                aggregates[tag] = lst
    # aggregate:all and aggregate:monitoring are deliberately never offered:
    # the global list is admin-managed (it used to be a pre-checked row, so
    # anyone subscribing for LAC silently landed on it), and the monitoring
    # list is the team's own.

    sections = "".join(_basin_section(b, page, by_iso3, aggregates) for b in BASINS)
    base = "../" if page == "staging" else ""
    robots = '<meta name="robots" content="noindex, nofollow">\n' if page == "staging" else ""
    staging_banner = (
        '<p class="staging">Staging copy for testing. Basins marked Live here may '
        'not be open on the <a href="../">public sign-up page</a> yet.</p>'
        if page == "staging" else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{robots}<title>Storm Alerts — Subscribe</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 15px;
         color: #1f2324; background: #f5f6f7; padding-bottom: 60px; line-height: 1.5; }}
  nav {{ background: #007eb5; padding: 11px 20px; display: flex; align-items: center;
         gap: 24px; font-size: 0.95em; }}
  nav .brand {{ color: #fff; font-weight: 700; margin-right: auto; }}
  nav a {{ color: #fff; text-decoration: none; opacity: 0.85; }}
  nav a.active {{ opacity: 1; font-weight: 700; border-bottom: 2px solid #fff; padding-bottom: 1px; }}
  .wrap {{ max-width: 800px; margin: 24px auto; padding: 0 16px; display: grid; gap: 16px; }}
  .card {{ background: #fff; border-radius: 8px; padding: 26px 28px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  h1 {{ font-size: 1.5em; color: #007eb5; margin-bottom: 6px; }}
  .lead {{ color: #4d585a; max-width: 62ch; }}
  .fields {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 18px; }}
  @media (max-width: 560px) {{ .fields {{ grid-template-columns: 1fr; }} .card {{ padding: 20px 16px; }} }}
  .field label {{ display: block; font-size: 0.82em; color: #5e6a6b; margin-bottom: 3px; }}
  .field input {{ width: 100%; padding: 8px 10px; border: 1px solid #c9d1d2; border-radius: 4px; font-size: 0.95em; }}
  .field input:focus, .tog:focus-visible, button[type=submit]:focus-visible {{ outline: 2px solid #007eb5; outline-offset: 1px; }}
  .basin {{ background: #fff; border-radius: 8px; padding: 20px 24px; box-shadow: 0 1px 4px rgba(0,0,0,.08);
            display: grid; gap: 12px; }}
  .basin header {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .basin h2 {{ font-size: 1.12em; }}
  .basin.dev {{ background: #fafbfb; box-shadow: none; border: 1px solid #e2e8e8; }}
  .basin.dev h2 {{ color: #5e6a6b; }}
  .pill {{ font-size: 0.72em; font-weight: 700; letter-spacing: .05em; text-transform: uppercase;
           padding: 3px 8px; border-radius: 3px; }}
  .pill.live {{ background: #e3f2e9; color: #276b43; }}
  .pill.dev {{ background: #eef1f2; color: #5e6a6b; }}
  .source {{ font-size: 0.86em; color: #5e6a6b; margin-top: -6px; }}
  .devnote {{ font-size: 0.9em; color: #4d585a; }}
  .bundle {{ background: #f0f7fb; border: 1px solid #b3d4e8; border-radius: 6px; padding: 10px 14px;
             display: grid; gap: 2px; }}
  .bundle label.cb {{ font-weight: 600; font-size: 0.95em; }}
  .bundle p {{ font-size: 0.82em; color: #5e6a6b; padding-left: 24px; }}
  fieldset {{ border: 1px solid #e2e8e8; border-radius: 6px; padding: 10px 14px 12px; }}
  legend {{ font-weight: 600; font-size: 0.9em; padding: 0 6px; display: flex; align-items: center; gap: 10px; }}
  .cb-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 2px 12px; margin-top: 6px; }}
  label.cb {{ display: flex; align-items: baseline; gap: 8px; font-size: 0.9em; cursor: pointer; padding: 3px 0; }}
  label.cb input {{ flex-shrink: 0; }}
  .tog {{ font-size: 0.78em; font-weight: 400; color: #007eb5; background: none; border: 1px solid #b3d4e8;
          border-radius: 3px; padding: 1px 7px; cursor: pointer; }}
  .tog:hover {{ background: #e8f4fb; }}
  .submit {{ display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }}
  button[type=submit] {{ background: #007eb5; color: #fff; border: none; border-radius: 5px; padding: 10px 28px;
                         font-size: 1em; cursor: pointer; }}
  button[type=submit]:hover {{ background: #005f8a; }}
  .note {{ font-size: 0.8em; color: #7e8e8f; }}
  .note a, .staging a {{ color: #007eb5; }}
  .staging {{ background: #fbf4ea; border-left: 4px solid #d48f2a; padding: 10px 14px; font-size: 0.9em; border-radius: 0 6px 6px 0; }}
</style>
</head>
<body>
<nav><span class="brand">Storm Alerts</span>
  <a href="{base}index.html" class="active">Subscribe</a><a href="{base}guide.html">About the alerts</a></nav>
<form class="wrap" method="post" action="{listmonk_url}/subscription/form">
  {staging_banner}
  <div class="card">
    <h1>Storm Alerts</h1>
    <p class="lead">Email alerts when a tropical cyclone forecast puts people in a
      country you follow inside its strong-wind zones. Countries are grouped by the
      ocean basin and forecast centre the alerts are based on.</p>
    <input type="hidden" name="nonce">
    <div class="fields">
      <div class="field"><label for="f-email">Email address *</label>
        <input id="f-email" type="email" name="email" required placeholder="you@example.com"></div>
      <div class="field"><label for="f-name">Name</label>
        <input id="f-name" type="text" name="name" placeholder="Optional"></div>
    </div>
  </div>
{sections}
  <div class="card submit">
    <button type="submit">Subscribe</button>
    <p class="note">You can unsubscribe at any time with the link in any alert.
      Alerts are sent by the <a href="https://centre.humdata.org">OCHA Centre for
      Humanitarian Data</a>.</p>
  </div>
</form>
<script>
  document.querySelectorAll('.tog').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const on = btn.dataset.state === '0';
      document.querySelectorAll('#region-' + btn.dataset.region + ' input').forEach(cb => {{ cb.checked = on; }});
      btn.dataset.state = on ? '1' : '0';
      btn.textContent = on ? 'Clear' : 'Select all';
    }});
  }});
</script>
</body>
</html>"""


def main() -> None:
    import os

    api_url = os.environ["DSCI_LISTMONK_BASE_URL"].rstrip("/")
    # Subscription form is at the listmonk root, not under /api
    root_url = api_url[:-4] if api_url.endswith("/api") else api_url
    client = ListmonkClient.from_env()
    all_lists = client.fetch_all_lists(tag=COUNTRY_LIST_TAG)
    print(f"Fetched {len(all_lists)} lists tagged '{COUNTRY_LIST_TAG}'")

    docs = Path(__file__).parents[1] / "docs"
    for page, out in (("public", docs / "index.html"),
                      ("staging", docs / "staging" / "index.html")):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(generate(root_url, all_lists, page), encoding="utf-8")
        print(f"Written: {out}")


if __name__ == "__main__":
    main()
