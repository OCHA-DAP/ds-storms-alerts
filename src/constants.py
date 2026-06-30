PROJECT_PREFIX = "ds-storms-alerts"

# "[TEST] Storm Alerts - Internal Test" — private Listmonk list (id 110), members:
# tristan.downing@un.org, downing.tristan@gmail.com, leonardo.milano@un.org.
# Test/preview sends go here instead of the live country lists.
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
