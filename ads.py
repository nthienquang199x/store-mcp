"""Google Ads API over REST: GAQL reads and atomic mutates (create/edit campaigns, ad groups, ads, budgets)."""
import json
import re

import httpx

from iap import google_client


def digits(customer_id: str) -> str:
    return re.sub(r"\D", "", customer_id or "")


def client(conf: dict, key_path: str) -> tuple[httpx.Client, str]:
    """conf = the [ads] section of apps.toml; key_path = service account JSON (added as a user in
    Google Ads) or an authorized_user JSON with a refresh token. Returns (client, API base url)."""
    c = google_client(key_path, "adwords")
    c.timeout = httpx.Timeout(120)
    c.headers["developer-token"] = conf["developer_token"]
    if conf.get("login_customer_id"):
        c.headers["login-customer-id"] = digits(conf["login_customer_id"])
    return c, f"https://googleads.googleapis.com/{conf.get('api_version', 'v25')}"


def req(c: httpx.Client, method: str, url: str, **kw):
    r = c.request(method, url, **kw)
    if r.status_code < 400:
        return r.json() if r.content else {}
    try:
        body = r.json()
        err = (body[0] if isinstance(body, list) else body)["error"]
        lines = [f"{e.get('errorCode')}: {e.get('message')}"
                 + (" @ " + ".".join(f"{p['fieldName']}" + (f"[{p['index']}]" if "index" in p else "")
                                     for p in e["location"].get("fieldPathElements", []))
                    if e.get("location") else "")
                 for d in err.get("details", []) for e in d.get("errors", [])]
        msg = "\n".join(lines) or err.get("message", "")
    except Exception:
        msg = r.text[:2000]
    raise RuntimeError(f"{method} {url.split('/customers')[-1]} -> {r.status_code}\n{msg}")


def flat(d: dict, prefix: str = ""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from flat(v, f"{prefix}{k}.")
        else:
            yield f"{prefix}{k}={json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v}"


def accounts(c: httpx.Client, base: str, conf: dict) -> str:
    mcc = digits(conf.get("login_customer_id", ""))
    if not mcc:
        names = req(c, "GET", f"{base}/customers:listAccessibleCustomers").get("resourceNames", [])
        return "\n".join(names) or "no accessible accounts"
    return query(c, base, mcc, "SELECT customer_client.id, customer_client.descriptive_name, customer_client.manager, "
                                "customer_client.status, customer_client.currency_code, customer_client.level "
                                "FROM customer_client")


def query(c: httpx.Client, base: str, cid: str, gaql: str, limit: int = 500) -> str:
    rows = [row for batch in req(c, "POST", f"{base}/customers/{cid}/googleAds:searchStream", json={"query": gaql})
            for row in batch.get("results", [])]
    out = [" ".join(flat(r)) for r in rows[:limit]]
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} more rows, add LIMIT or filters")
    return "\n".join(out) or "no rows"


def guard(ops: list[dict], allow_enable: bool) -> list[str]:
    """New campaigns default to PAUSED; turning a campaign on (spending money) needs allow_enable."""
    notes = []
    for i, op in enumerate(ops):
        body = op.get("campaignOperation", {})
        if "create" in body and "status" not in body["create"]:
            body["create"]["status"] = "PAUSED"
            notes.append(f"op {i}: new campaign set to PAUSED")
        for verb in ("create", "update"):
            if body.get(verb, {}).get("status") == "ENABLED" and not allow_enable:
                raise ValueError(f"op {i} enables a campaign (starts spending); pass allow_enable=true "
                                 "only after the user explicitly approves")
    return notes


def mutate(c: httpx.Client, base: str, cid: str, ops: list[dict], dry_run: bool, allow_enable: bool) -> str:
    notes = guard(ops, allow_enable)
    r = req(c, "POST", f"{base}/customers/{cid}/googleAds:mutate",
            json={"mutateOperations": ops, "validateOnly": dry_run})
    if dry_run:
        return "\n".join(notes + [f"valid: {len(ops)} operation(s), nothing changed (dry_run). "
                                  "Re-run with dry_run=false to apply."])
    done = [" ".join(flat(x)) for x in r.get("mutateOperationResponses", [])]
    return "\n".join(notes + done)


if __name__ == "__main__":
    ops = [{"campaignOperation": {"create": {"name": "x"}}}]
    assert guard(ops, False) and ops[0]["campaignOperation"]["create"]["status"] == "PAUSED"
    for op in ({"create": {"status": "ENABLED"}}, {"update": {"status": "ENABLED"}, "updateMask": "status"}):
        try:
            guard([{"campaignOperation": op}], False)
            raise AssertionError("enable not blocked")
        except ValueError:
            pass
    assert not guard([{"campaignOperation": {"update": {"status": "ENABLED"}}}], True)
    assert list(flat({"campaign": {"name": "a", "id": "1"}, "metrics": {"clicks": "3"}})) == \
        ["campaign.name=a", "campaign.id=1", "metrics.clicks=3"]
    print("ok")
