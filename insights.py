"""Vitals, testers, subscriptions and orders (read-mostly) for App Store Connect and Google Play."""
import datetime as dt
import time

import httpx

from iap import PLAY, asc_pages, call, money
from release import play_tracks

REPORTING = "https://playdeveloperreporting.googleapis.com/v1beta1/apps"


# ---------------------------------------------------------------- vitals

def android_vitals(c: httpx.Client, package: str) -> str:
    lines = []
    for metric_set, metrics in (("crashRateMetricSet", ["crashRate28dUserWeighted",
                                                         "userPerceivedCrashRate28dUserWeighted"]),
                                ("anrRateMetricSet", ["anrRate28dUserWeighted",
                                                      "userPerceivedAnrRate28dUserWeighted"])):
        info = call(c, "GET", f"{REPORTING}/{package}/{metric_set}")
        end = next(f["latestEndTime"] for f in info["freshnessInfo"]["freshnesses"]
                   if f["aggregationPeriod"] == "DAILY")
        day = dt.date(end["year"], end["month"], end["day"]) - dt.timedelta(days=1)
        when = {"year": day.year, "month": day.month, "day": day.day, "timeZone": end["timeZone"]}
        rows = call(c, "POST", f"{REPORTING}/{package}/{metric_set}:query", json={
            "timelineSpec": {"aggregationPeriod": "DAILY", "startTime": when, "endTime": end},
            "metrics": metrics + ["distinctUsers"]}).get("rows", [])
        for m in (rows[0]["metrics"] if rows else []):
            v = m.get("decimalValue", {}).get("value", "?")
            lines.append(f"{m['metric']}: {float(v):.3%}" if "Rate" in m["metric"] and v != "?"
                         else f"{m['metric']}: {v}")
    lines.append("(Play bad-behavior thresholds: user-perceived crash 1.09%, ANR 0.47%)")
    issues = call(c, "GET", f"{REPORTING}/{package}/errorIssues:search?pageSize=10&orderBy=distinctUsers desc")
    lines.append("Top issues (by users):")
    for i in issues.get("errorIssues", []):
        lines.append(f"  {i['type']} users={i.get('distinctUsers')} reports={i.get('errorReportCount')} "
                     f"v{i.get('lastAppVersion', {}).get('versionCode', '?')}: {i.get('cause', '')[:120]}"
                     f" @ {i.get('location', '')[:80]}")
    return "\n".join(lines)


def ios_vitals(c: httpx.Client, app_id: str) -> str:
    r = c.get(f"/v1/apps/{app_id}/perfPowerMetrics?filter[platform]=IOS",
              headers={"Accept": "application/vnd.apple.xcode-metrics+json"})
    if r.status_code >= 400:
        raise RuntimeError(f"perfPowerMetrics -> {r.status_code}\n{r.text[:800]}")
    lines = ["Xcode metrics (latest version, all iPhones, p50 where available):"]
    for product in r.json().get("productData", []):
        for cat in product.get("metricCategories", []):
            for m in cat.get("metrics", []):
                ds = m.get("datasets", [])
                d = next((x for x in ds if x.get("filterCriteria", {}).get("percentile") == "p50"), ds[0] if ds else None)
                if d and d.get("points"):
                    pt = d["points"][-1]
                    lines.append(f"  {cat['identifier']}/{m['identifier']}: {pt.get('value')} "
                                 f"{m.get('unit', {}).get('displayName', '')} (v{pt.get('version')})")
    if len(lines) == 1:
        lines.append("  no data yet (Apple needs enough users who share analytics)")
    builds = call(c, "GET", f"/v1/builds?filter[app]={app_id}&sort=-uploadedDate&limit=1")["data"]
    if builds:
        try:
            sigs = call(c, "GET", f"/v1/builds/{builds[0]['id']}/diagnosticSignatures?limit=10")["data"]
        except RuntimeError:
            sigs = []  # 404 until Apple has collected diagnostics for the build
        lines.append(f"Diagnostic signatures for build {builds[0]['attributes']['version']} (hangs, launches, disk writes):")
        lines += [f"  {s['attributes']['diagnosticType']} weight={s['attributes']['weight']:.1%}: "
                  f"{s['attributes']['signature'][:120]}" for s in sigs] or ["  none"]
    lines.append("(Crash counts are not in the App Store Connect API; use Xcode Organizer or Crashlytics.)")
    return "\n".join(lines)


# ---------------------------------------------------------------- testers

def ios_testers(c: httpx.Client, app_id: str, action: str, group: str, emails: list[str]) -> str:
    groups = asc_pages(c, f"/v1/apps/{app_id}/betaGroups?limit=200")
    if action == "list" and not group:
        out = []
        for g in groups:
            n = call(c, "GET", f"/v1/betaGroups/{g['id']}/betaTesters?limit=1")["meta"]["paging"]["total"]
            a = g["attributes"]
            out.append(f"{a['name']}: {n} testers, {'internal' if a.get('isInternalGroup') else 'external'}"
                       + (f", public link {a['publicLink']}" if a.get("publicLinkEnabled") else ""))
        return "\n".join(out) or "no TestFlight groups"
    g = next((x for x in groups if x["attributes"]["name"] == group), None)
    if not g:
        raise ValueError(f"no TestFlight group '{group}', have: {', '.join(x['attributes']['name'] for x in groups)}")
    members = asc_pages(c, f"/v1/betaGroups/{g['id']}/betaTesters?limit=200")
    if action == "list":
        return "\n".join(f"{t['attributes'].get('email')} {t['attributes'].get('state', '')}" for t in members) or "empty"
    by_email = {t["attributes"].get("email", "").lower(): t["id"] for t in members}
    if action == "remove":
        ids = [by_email[e.lower()] for e in emails if e.lower() in by_email]
        if ids:
            call(c, "DELETE", f"/v1/betaGroups/{g['id']}/relationships/betaTesters",
                 json={"data": [{"type": "betaTesters", "id": i} for i in ids]})
        return f"removed {len(ids)} of {len(emails)} from {group}"
    if action != "add":
        raise ValueError("action must be list, add or remove")
    added = 0
    for e in emails:
        if e.lower() in by_email:
            continue
        existing = call(c, "GET", f"/v1/betaTesters?filter[email]={e}&limit=1")["data"]
        if existing:
            call(c, "POST", f"/v1/betaGroups/{g['id']}/relationships/betaTesters",
                 json={"data": [{"type": "betaTesters", "id": existing[0]["id"]}]})
        else:
            call(c, "POST", "/v1/betaTesters", json={"data": {
                "type": "betaTesters", "attributes": {"email": e},
                "relationships": {"betaGroups": {"data": [{"type": "betaGroups", "id": g["id"]}]}}}})
        added += 1
    return f"added {added} to {group} ({len(emails) - added} already in it)"


def android_testers(c: httpx.Client, package: str, action: str, track: str, emails: list[str]) -> str:
    """Closed-testing tracks only, and only Google Groups (the API has no email lists)."""
    # Play allows one open edit per app, so read the track list before opening ours.
    tracks = [track] if track else [t["track"] for t in play_tracks(c, package) if t["track"] != "production"]
    edit = call(c, "POST", f"{PLAY}/{package}/edits")["id"]
    base = f"{PLAY}/{package}/edits/{edit}/testers"
    try:
        if action == "list":
            return "\n".join(f"{t}: {', '.join(call(c, 'GET', f'{base}/{t}').get('googleGroups', [])) or 'none'}"
                             for t in tracks)
        if action not in ("add", "remove") or not track:
            raise ValueError("action must be list, add or remove; add/remove need a track")
        cur = call(c, "GET", f"{base}/{track}").get("googleGroups", [])
        new = sorted(set(cur) | set(emails)) if action == "add" else [g for g in cur if g not in emails]
        call(c, "PUT", f"{base}/{track}", json={"googleGroups": new})
        call(c, "POST", f"{PLAY}/{package}/edits/{edit}:commit")
        edit = None
        return f"{track} google groups: {', '.join(new) or 'none'}"
    finally:
        if edit:
            c.delete(f"{PLAY}/{package}/edits/{edit}")


# ---------------------------------------------------------------- subscriptions

def ios_subscriptions(c: httpx.Client, app_id: str) -> str:
    today = time.strftime("%Y-%m-%d")
    out = []
    for g in asc_pages(c, f"/v1/apps/{app_id}/subscriptionGroups?limit=50"):
        out.append(f"group {g['attributes']['referenceName']}:")
        for s in asc_pages(c, f"/v1/subscriptionGroups/{g['id']}/subscriptions?limit=50"):
            a = s["attributes"]
            r = call(c, "GET", f"/v1/subscriptions/{s['id']}/prices?filter[territory]=USA"
                               "&include=subscriptionPricePoint&limit=50")
            points = {i["id"]: i["attributes"]["customerPrice"] for i in r.get("included", [])
                      if i["type"] == "subscriptionPricePoints"}
            current = [p for p in r["data"] if (p["attributes"].get("startDate") or "") <= today]
            current.sort(key=lambda p: p["attributes"].get("startDate") or "")
            price = points.get(current[-1]["relationships"]["subscriptionPricePoint"]["data"]["id"]) if current else None
            out.append(f"  {a['productId']} ({a.get('name')}): {a.get('subscriptionPeriod')} "
                       f"${price or '?'} level={a.get('groupLevel')} {a.get('state')}")
    return "\n".join(out) or "no subscriptions"


def android_subscriptions(c: httpx.Client, package: str) -> str:
    out = []
    for s in call(c, "GET", f"{PLAY}/{package}/subscriptions?pageSize=100").get("subscriptions", []):
        title = (s.get("listings") or [{}])[0].get("title", "")
        out.append(f"{s['productId']} ({title}):")
        for bp in s.get("basePlans", []):
            us = next((r["price"] for r in bp.get("regionalConfigs", []) if r["regionCode"] == "US"), None)
            period = (bp.get("autoRenewingBasePlanType") or bp.get("prepaidBasePlanType")
                      or bp.get("installmentsBasePlanType") or {}).get("billingPeriodDuration", "?")
            out.append(f"  {bp['basePlanId']}: {period} ${money(us) if us else '?'} {bp.get('state')}")
    return "\n".join(out) or "no subscriptions"


# ---------------------------------------------------------------- orders

VOID_REASON = {0: "other", 1: "remorse", 2: "not received", 3: "defective", 4: "accidental purchase",
               5: "fraud", 6: "friendly fraud", 7: "chargeback", 8: "unacknowledged"}


def android_orders(c: httpx.Client, package: str, order_id: str, voided_days: int) -> str:
    if order_id:
        o = call(c, "GET", f"{PLAY}/{package}/orders/{order_id}")
        items = ", ".join(f"{i.get('productId')} {money(i['total'])} {i['total']['currencyCode']}"
                          for i in o.get("lineItems", []) if "total" in i)
        return (f"{o['orderId']} {o.get('state')} created={o.get('createTime')} "
                f"total={money(o['total'])} {o['total']['currencyCode']}\n  items: {items}"
                f"\n  purchaseToken: {o.get('purchaseToken')}")
    start = int((time.time() - min(voided_days, 30) * 86400 + 600) * 1000)  # Play keeps 30 days
    r = call(c, "GET", f"{PLAY}/{package}/purchases/voidedpurchases?startTime={start}&maxResults=100&type=1")
    out = [f"{v['orderId']} voided={time.strftime('%Y-%m-%d', time.gmtime(int(v['voidedTimeMillis']) / 1000))} "
           f"reason={VOID_REASON.get(v.get('voidedReason'), v.get('voidedReason'))}"
           for v in r.get("voidedPurchases", [])]
    return "\n".join(out) or f"no refunded/voided purchases in the last {voided_days} days"
