"""AdMob API over REST: inventory and reports (changes go through browser.py). Also the OAuth login CLI
(AdMob does not accept service accounts, so it needs a user refresh token)."""
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

from iap import google_client

API = "https://admob.googleapis.com"
SCOPES = ["https://www.googleapis.com/auth/admob.readonly",
          "https://www.googleapis.com/auth/admob.monetization",
          "https://www.googleapis.com/auth/adwords"]  # one login also covers the Google Ads tools

REPORTS = {  # kind: (endpoint, dimensions, metrics)
    "network": ("v1/{parent}/networkReport:generate",
                "DATE MONTH WEEK AD_UNIT APP AD_TYPE COUNTRY FORMAT PLATFORM MOBILE_OS_VERSION GMA_SDK_VERSION "
                "APP_VERSION_NAME SERVING_RESTRICTION",
                "AD_REQUESTS CLICKS ESTIMATED_EARNINGS IMPRESSIONS IMPRESSION_CTR IMPRESSION_RPM MATCHED_REQUESTS "
                "MATCH_RATE SHOW_RATE"),
    "mediation": ("v1/{parent}/mediationReport:generate",
                  "DATE MONTH WEEK AD_SOURCE AD_SOURCE_INSTANCE AD_UNIT APP MEDIATION_GROUP COUNTRY FORMAT PLATFORM "
                  "MOBILE_OS_VERSION GMA_SDK_VERSION APP_VERSION_NAME SERVING_RESTRICTION",
                  "AD_REQUESTS CLICKS ESTIMATED_EARNINGS IMPRESSIONS IMPRESSION_CTR MATCHED_REQUESTS MATCH_RATE "
                  "OBSERVED_ECPM"),
    "campaign": ("v1beta/{parent}/campaignReport:generate",
                 "DATE CAMPAIGN_ID CAMPAIGN_NAME AD_ID AD_NAME PLACEMENT_ID PLACEMENT_NAME PLACEMENT_PLATFORM "
                 "COUNTRY FORMAT",
                 "IMPRESSIONS CLICKS CLICK_THROUGH_RATE INSTALLS ESTIMATED_COST AVERAGE_CPI INTERACTIONS"),
}


def req(c: httpx.Client, method: str, path: str, **kw):
    r = c.request(method, f"{API}/{path}", **kw)
    if r.status_code < 400:
        return r.json() if r.content else {}
    try:
        msg = (r.json()[0] if r.text.startswith("[") else r.json())["error"]["message"]
    except Exception:
        msg = r.text[:1500]
    hint = {401: " (run store-mcp-login again; Testing-mode OAuth apps expire tokens after 7 days)",
            429: " (quota: narrow the date range or dimensions)"}.get(r.status_code, "")
    raise RuntimeError(f"{method} {path.split('?')[0]} -> {r.status_code}: {msg}{hint}")


def pages(c: httpx.Client, path: str, key: str) -> list[dict]:
    out, token = [], ""
    while True:
        sep = "&" if "?" in path else "?"
        r = req(c, "GET", f"{path}{sep}pageSize=1000" + (f"&pageToken={token}" if token else ""))
        out += r.get(key, [])
        token = r.get("nextPageToken")
        if not token:
            return out


def account(c: httpx.Client, publisher_id: str) -> str:
    if publisher_id:
        return publisher_id if publisher_id.startswith("accounts/") else f"accounts/{publisher_id}"
    accts = req(c, "GET", "v1/accounts").get("account", [])
    if not accts:
        raise RuntimeError("no AdMob account for this login")
    return accts[0]["name"]


# ---------------------------------------------------------------- reads

def inventory(c: httpx.Client, parent: str) -> str:
    a = req(c, "GET", f"v1/{parent}")
    lines = [f"{a['name']} {a.get('currencyCode')} {a.get('reportingTimeZone')}"]
    units = {}
    for u in pages(c, f"v1/{parent}/adUnits", "adUnits"):
        units.setdefault(u["appId"], []).append(u)
    for app in pages(c, f"v1/{parent}/apps", "apps"):
        info = app.get("linkedAppInfo") or app.get("manualAppInfo") or {}
        lines.append(f"{app['appId']} {app.get('platform')} {info.get('displayName', '')} "
                     f"store={info.get('appStoreId', 'unlinked')} {app.get('appApprovalState', '')}")
        lines += [f"  {u['adUnitId']} {u.get('adFormat')} {u.get('displayName')}"
                  for u in units.get(app["appId"], [])]
    return "\n".join(lines)


def value(v: dict) -> str:
    if "microsValue" in v:
        return f"{int(v['microsValue']) / 1e6:.2f}"
    if "doubleValue" in v:
        return f"{v['doubleValue']:.4g}"
    return str(v.get("integerValue", v.get("value", "?")))


def report(c: httpx.Client, parent: str, kind: str, start: str, end: str, dimensions: list[str],
           metrics: list[str], filters: dict[str, list[str]], sort: str, limit: int) -> str:
    if kind not in REPORTS:
        raise ValueError(f"kind must be one of {', '.join(REPORTS)}")
    path, dims_ok, mets_ok = REPORTS[kind]
    bad = [d for d in dimensions + list(filters) if d not in dims_ok.split()] + \
          [m for m in metrics if m not in mets_ok.split()]
    if bad or not metrics:
        raise ValueError(f"invalid/missing: {bad or 'metrics'}. {kind} dimensions: {dims_ok}. metrics: {mets_ok}")
    day = lambda s: dict(zip(("year", "month", "day"), map(int, s.split("-"))))  # noqa: E731
    spec = {"dateRange": {"startDate": day(start), "endDate": day(end)},
            "dimensions": dimensions, "metrics": metrics}
    if filters:
        spec["dimensionFilters"] = [{"dimension": d, "matchesAny": {"values": v}} for d, v in filters.items()]
    if sort:
        key, _, order = sort.partition(" ")
        spec["sortConditions"] = [{"metric" if key in mets_ok.split() else "dimension": key,
                                   "order": "ASCENDING" if order.lower().startswith("asc") else "DESCENDING"}]
    data = req(c, "POST", path.format(parent=parent), json={"reportSpec": spec})
    rows = [m["row"] for m in data if "row" in m] if isinstance(data, list) else data.get("rows", [])
    out = []
    for r in rows[:limit]:
        dv = " ".join(f"{k}={v.get('displayLabel') or v.get('value')}" for k, v in r.get("dimensionValues", {}).items())
        mv = " ".join(f"{k}={value(v)}" for k, v in r.get("metricValues", {}).items())
        out.append(f"{dv} | {mv}" if dv else mv)
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} more rows (raise limit or narrow the report)")
    return "\n".join(out) or "no rows"


# ---------------------------------------------------------------- login CLI

def login():
    """store-mcp-login <oauth-desktop-client.json> [out.json]: loopback + PKCE, saves an authorized_user JSON."""
    if len(sys.argv) < 2:
        sys.exit("usage: store-mcp-login <oauth-desktop-client.json> [out=~/.config/store-mcp/google_oauth.json]")
    client = json.loads(Path(sys.argv[1]).expanduser().read_text())
    client = client.get("installed") or client.get("web") or client
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "~/.config/store-mcp/google_oauth.json").expanduser()
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(16)
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state:
                got.update(q)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Done, you can close this tab." if "code" in got else b"Login failed.")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge_method": "S256",
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()})
    print(f"Opening browser. If it does not open, visit:\n{url}", flush=True)
    webbrowser.open(url)
    while not got:
        srv.handle_request()
    if "code" not in got:
        sys.exit(f"login failed: {got.get('error')}")
    tok = httpx.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
        "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
        "code_verifier": verifier}, timeout=30)
    tok.raise_for_status()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"type": "authorized_user", "client_id": client["client_id"],
                               "client_secret": client.get("client_secret", ""),
                               "refresh_token": tok.json()["refresh_token"]}, indent=1))
    os.chmod(out, 0o600)
    print(f"saved {out}")


def client(key_path: str) -> httpx.Client:
    return google_client(key_path, "admob.readonly")


if __name__ == "__main__":
    assert value({"microsValue": "1234567"}) == "1.23" and value({"integerValue": "5"}) == "5"
    print("ok")
