"""Render the alert body through the real Listmonk campaign template.

The point is to iterate on the email without sending one. Listmonk only
renders a template server-side in the context of a *campaign*, so this
module keeps exactly one long-lived draft campaign — `PREVIEW_CAMPAIGN_NAME`
— and pushes bodies through its preview endpoint:

    POST /api/campaigns/{id}/preview   body=<our html>

That call renders and returns; it does **not** store the body, so the draft
stays tiny no matter how many previews you run. Nothing here ever calls
Listmonk's send endpoint, and the draft is parked on the internal test list
(`TEST_LIST_ID`) rather than any country list — Listmonk rejects a campaign
with no lists at all, so "attached to nobody" is not available.

Two things come from the stored campaign rather than the POST payload, so
they are PUT onto the draft before each preview:

- **name** — the OCHA template branches on it (`contains "[test]"` picks the
  test-variant banner, `[fr]`/`[es]` pick translations). Previewing under a
  name that does not match the real campaign name shows the wrong chrome.
- **subject** — the template renders it as the `<h1>` header bar.

Listmonk ignores a `subject` field posted to the preview endpoint; only the
stored one is used. That is why the PUT is not optional.
"""

from __future__ import annotations

import logging
import re

import requests
from ocha_relay.listmonk import DEFAULT_CAMPAIGN_TEMPLATE_ID, ListmonkClient

from src.constants import TEST_LIST_IDS

logger = logging.getLogger(__name__)

# One reusable draft, found by exact name. The "[test]" prefix is load-bearing
# twice over: it selects the template's test variant, and it makes the campaign
# obvious in the Listmonk UI as tooling rather than a real send.
#
# No brackets or parentheses beyond the "[test]" tag: Listmonk's `query`
# parameter is fed to Postgres `to_tsquery`, so punctuation in the search term
# comes back as a 500, not as zero results.
PREVIEW_CAMPAIGN_NAME = "[test] ds-storms-alerts preview scratch - do not send"
_SEARCH_TOKEN = "scratch"


class PreviewUnavailable(RuntimeError):
    """Listmonk could not be reached or is not configured."""


def _find_campaign_id(client: ListmonkClient, name: str) -> int | None:
    """Exact-name lookup, narrowed by a full-text token.

    `query` is a Postgres full-text match, not a substring match, so it can
    only narrow the candidate set — the exact name still has to be checked
    here. The instance carries >1000 campaigns, so paging the whole list
    instead is not an option.
    """
    r = requests.get(
        f"{client.base_url}/campaigns",
        auth=client._auth,
        params={"query": _SEARCH_TOKEN, "per_page": 100, "page": 1,
                "no_body": "true"},
        timeout=client.timeout,
    )
    r.raise_for_status()
    for c in r.json()["data"]["results"]:
        if c["name"] == name:
            return c["id"]
    return None


def _set_name_and_subject(
    client: ListmonkClient, campaign_id: int, name: str, subject: str
) -> None:
    """PUT replaces the whole campaign, so the current record is read first and
    only the two fields we care about are swapped — a partial PUT would blank
    the body and drop the template."""
    cur = client.get_campaign(campaign_id)
    payload = {
        "name": name,
        "subject": subject,
        # Listmonk rejects a campaign with no lists, so park it on the internal
        # test list. Nothing in this module calls the send endpoint.
        "lists": TEST_LIST_IDS,
        "template_id": cur.get("template_id") or DEFAULT_CAMPAIGN_TEMPLATE_ID,
        "content_type": cur.get("content_type") or "html",
        "type": "regular",
        "body": cur.get("body") or "",
    }
    r = requests.put(
        f"{client.base_url}/campaigns/{campaign_id}",
        auth=client._auth,
        json=payload,
        timeout=client.timeout,
    )
    r.raise_for_status()


def render_with_template(
    body: str,
    subject: str,
    campaign_name: str,
    *,
    client: ListmonkClient | None = None,
    template_id: int = DEFAULT_CAMPAIGN_TEMPLATE_ID,
) -> str:
    """Return `body` wrapped in the Listmonk campaign template, as sent.

    `campaign_name` is the name the real send would use; it is applied to the
    preview draft so the template's name-based branching matches production.
    Raises :class:`PreviewUnavailable` if Listmonk is unreachable or the
    credentials are missing.
    """
    try:
        client = client or ListmonkClient.from_env()
    except Exception as exc:  # missing env vars
        raise PreviewUnavailable(str(exc)) from exc

    try:
        cid = _find_campaign_id(client, PREVIEW_CAMPAIGN_NAME)
        if cid is None:
            cid = client.create_campaign(
                name=PREVIEW_CAMPAIGN_NAME,
                subject=subject,
                body="",
                list_ids=TEST_LIST_IDS,
                template_id=template_id,
            )
            logger.info(f"Created preview draft campaign {cid}")
        _set_name_and_subject(client, cid, campaign_name, subject)

        r = requests.post(
            f"{client.base_url}/campaigns/{cid}/preview",
            auth=client._auth,
            data={
                "body": body,
                "template_id": template_id,
                "content_type": "html",
            },
            timeout=client.timeout,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        raise PreviewUnavailable(f"Listmonk request failed: {exc}") from exc

    # Restore the parking name so a stray glance at the Listmonk UI does not
    # show something that reads like a real pending campaign.
    try:
        _set_name_and_subject(
            client, cid, PREVIEW_CAMPAIGN_NAME, "(preview scratch campaign)"
        )
    except requests.RequestException:
        logger.warning("Could not reset the preview draft's name; harmless.")

    return _fix_preview_charset(r.text)


# The OCHA template hard-codes `charset=us-ascii` in its <meta>. A real send is
# unaffected — the MIME Content-Type header carries the true charset and wins —
# but a browser opening this HTML has only the meta to go on, so every em dash
# and non-breaking space renders as mojibake. Patch it for display only; the
# body that gets sent is untouched.
_CHARSET_META = re.compile(r'(<meta[^>]+charset=)us-ascii', re.IGNORECASE)


def _fix_preview_charset(html: str) -> str:
    return _CHARSET_META.sub(r"\1utf-8", html, count=1)
