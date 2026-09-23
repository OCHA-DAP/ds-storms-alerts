PROJECT_PREFIX = "ds-storms-alerts"

# "[TEST] Storm Alerts - Internal Test" — private Listmonk list (id 110).
# Sole member: tristan.downing@un.org (trimmed 2026-08-31 so test sends
# reach only the operator). Test/preview sends go here instead of the
# live country lists.
TEST_LIST_ID = 110
TEST_LIST_IDS = [TEST_LIST_ID]

COUNTRY_LIST_TAG = "ds-storms-alerts"
COUNTRY_LIST_NAME_PREFIX = "Storm Alerts"  # list display name: "Storm Alerts - Haiti"

# Caribbean + Central America + South/North America, excluding USA and Canada.
# Subscribers on this list receive every email that includes at least one LAC country.
LAC_ISO3S: frozenset[str] = frozenset({
    "ABW", "AIA", "ATG", "BES", "BHS", "BLM", "BLZ", "BMU", "BRB",
    "COL", "CRI", "CUB", "CUW", "CYM", "DMA", "DOM",
    "GLP", "GRD", "GTM", "GUF", "GUY",
    "HND", "HTI", "JAM", "KNA", "LCA", "MAF", "MEX", "MSR", "MTQ",
    "NIC", "PAN", "PRI", "SLV", "SPM", "SUR", "SXM",
    "TCA", "TTO", "VCT", "VEN", "VGB", "VIR",
})

# SES (direct SMTP) backend — stop-gap while Listmonk is down (it runs on the
# dev DB, which lost public network access on 2026-09-22). Selected with
# EMAIL_BACKEND=ses; recipients can be overridden with SES_RECIPIENTS
# (comma-separated). See src/ses_mail.py.
SES_RECIPIENTS_LIVE = [
    "tristan.downing@un.org",
    "zachary.arno@un.org",
    "leonardo.milano@un.org",
]
SES_RECIPIENTS_TEST = ["tristan.downing@un.org"]
