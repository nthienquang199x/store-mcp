"""One-time in-app purchases (consumable / non-consumable) via App Store Connect API and
Google Play Developer API (monetization.onetimeproducts).

Products use one shared file format, <root>/<app>/iap/<platform>/<product_id>.json:
{
  "product_id": "coins_100",
  "type": "consumable",            # consumable | non_consumable | non_renewing (iOS only)
  "reference_name": "100 Coins",   # iOS only
  "price_usd": "0.99",             # other regions are derived by Apple / Play
  "listings": {"en-US": {"title": "100 Coins", "description": "A bag of coins"}},
  "review_note": "",               # iOS only
  "review_screenshot": "",         # iOS only, image path relative to the json file
  "family_sharable": false,        # iOS only
  "state": "APPROVED"              # read-only
}
"""
import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path

import httpx
import jwt

ASC = "https://api.appstoreconnect.apple.com"
PLAY = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
IOS_TYPES = {"consumable": "CONSUMABLE", "non_consumable": "NON_CONSUMABLE",
             "non_renewing": "NON_RENEWING_SUBSCRIPTION"}


def call(client: httpx.Client, method: str, url: str, **kw) -> dict:
    r = client.request(method, url, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {url.split('?')[0]} -> {r.status_code}\n{r.text[:1500]}")
    return r.json() if r.content else {}


def compact(p: dict) -> dict:
    return {k: v for k, v in p.items() if v not in (None, "", {}, False) or k == "listings"}


# ---------------------------------------------------------------- App Store Connect

def asc_client(key_path: str) -> httpx.Client:
    k = json.loads(Path(key_path).read_text())
    now = int(time.time())
    token = jwt.encode({"iss": k["issuer_id"], "iat": now, "exp": now + 1200,
                        "aud": "appstoreconnect-v1"},
                       k["key"], algorithm="ES256", headers={"kid": k["key_id"]})
    return httpx.Client(base_url=ASC, headers={"Authorization": f"Bearer {token}"}, timeout=60)


def asc_pages(c: httpx.Client, url: str) -> list[dict]:
    out = []
    while url:
        r = call(c, "GET", url)
        out += r["data"]
        url = r.get("links", {}).get("next")
    return out


def asc_app_id(c: httpx.Client, bundle_id: str) -> str:
    apps = call(c, "GET", f"/v1/apps?filter[bundleId]={bundle_id}")["data"]
    if not apps:
        raise ValueError(f"no App Store Connect app with bundle id {bundle_id}")
    return apps[0]["id"]


def asc_usd_price(c: httpx.Client, iap_id: str) -> str | None:
    try:
        sched = call(c, "GET", f"/v2/inAppPurchases/{iap_id}/iapPriceSchedule")["data"]["id"]
    except RuntimeError:
        return None  # no price set yet
    today = time.strftime("%Y-%m-%d")
    for kind in ("manualPrices", "automaticPrices"):
        r = call(c, "GET", f"/v1/inAppPurchasePriceSchedules/{sched}/{kind}?filter[territory]=USA"
                 "&include=inAppPurchasePricePoint&limit=50")
        points = {i["id"]: i["attributes"]["customerPrice"] for i in r.get("included", [])
                  if i["type"] == "inAppPurchasePricePoints"}
        for p in r["data"]:
            a = p["attributes"]
            if (a.get("startDate") or "") <= today and (not a.get("endDate") or a["endDate"] > today):
                return points.get(p["relationships"]["inAppPurchasePricePoint"]["data"]["id"])
    return None


def ios_remote(c: httpx.Client, app_id: str) -> dict[str, dict]:
    """productId -> {"id": asc id, "localization_ids": {...}, **file format}."""
    out = {}
    for iap in asc_pages(c, f"/v1/apps/{app_id}/inAppPurchasesV2?limit=200"):
        a, iid = iap["attributes"], iap["id"]
        locs = call(c, "GET", f"/v2/inAppPurchases/{iid}/inAppPurchaseLocalizations?limit=50")["data"]
        out[a["productId"]] = {
            "id": iid,
            "localization_ids": {l["attributes"]["locale"]: l["id"] for l in locs},
            "product_id": a["productId"],
            "type": {v: k for k, v in IOS_TYPES.items()}.get(a["inAppPurchaseType"], a["inAppPurchaseType"]),
            "reference_name": a.get("name"),
            "price_usd": asc_usd_price(c, iid),
            "listings": {l["attributes"]["locale"]: {"title": l["attributes"].get("name") or "",
                                                     "description": l["attributes"].get("description") or ""}
                         for l in locs},
            "review_note": a.get("reviewNote"),
            "family_sharable": a.get("familySharable"),
            "state": a.get("state"),
            "has_screenshot": asc_has_screenshot(c, iid),
        }
    return out


def asc_has_screenshot(c: httpx.Client, iap_id: str) -> bool:
    try:
        return bool(call(c, "GET", f"/v2/inAppPurchases/{iap_id}/appStoreReviewScreenshot").get("data"))
    except RuntimeError:
        return False


def asc_price_point(c: httpx.Client, iap_id: str, usd: str) -> str:
    points = asc_pages(c, f"/v2/inAppPurchases/{iap_id}/pricePoints?filter[territory]=USA&limit=8000")
    for p in points:
        if Decimal(p["attributes"]["customerPrice"]) == Decimal(usd):
            return p["id"]
    near = sorted(points, key=lambda p: abs(Decimal(p["attributes"]["customerPrice"]) - Decimal(usd)))[:5]
    raise ValueError(f"no USA price point {usd}; nearest: {[p['attributes']['customerPrice'] for p in near]}")


def asc_set_price(c: httpx.Client, iap_id: str, usd: str):
    # ponytail: base territory USA, Apple auto-equalizes the rest; per-territory manual prices get replaced
    point = asc_price_point(c, iap_id, usd)
    call(c, "POST", "/v1/inAppPurchasePriceSchedules", json={
        "data": {"type": "inAppPurchasePriceSchedules", "relationships": {
            "inAppPurchase": {"data": {"type": "inAppPurchases", "id": iap_id}},
            "baseTerritory": {"data": {"type": "territories", "id": "USA"}},
            "manualPrices": {"data": [{"type": "inAppPurchasePrices", "id": "${p0}"}]}}},
        "included": [{"type": "inAppPurchasePrices", "id": "${p0}", "attributes": {"startDate": None},
                      "relationships": {"inAppPurchasePricePoint": {
                          "data": {"type": "inAppPurchasePricePoints", "id": point}}}}]})


def asc_set_availability(c: httpx.Client, iap_id: str):
    territories = asc_pages(c, "/v1/territories?limit=200")
    call(c, "POST", "/v1/inAppPurchaseAvailabilities", json={
        "data": {"type": "inAppPurchaseAvailabilities", "attributes": {"availableInNewTerritories": True},
                 "relationships": {
                     "inAppPurchase": {"data": {"type": "inAppPurchases", "id": iap_id}},
                     "availableTerritories": {"data": [{"type": "territories", "id": t["id"]}
                                                       for t in territories]}}}})


def asc_upload_screenshot(c: httpx.Client, iap_id: str, image: Path):
    data = image.read_bytes()
    res = call(c, "POST", "/v1/inAppPurchaseAppStoreReviewScreenshots", json={
        "data": {"type": "inAppPurchaseAppStoreReviewScreenshots",
                 "attributes": {"fileName": image.name, "fileSize": len(data)},
                 "relationships": {"inAppPurchaseV2": {"data": {"type": "inAppPurchases", "id": iap_id}}}}})["data"]
    for op in res["attributes"]["uploadOperations"]:
        r = httpx.request(op["method"], op["url"], content=data[op["offset"]:op["offset"] + op["length"]],
                          headers={h["name"]: h["value"] for h in op["requestHeaders"]}, timeout=120)
        r.raise_for_status()
    call(c, "PATCH", f"/v1/inAppPurchaseAppStoreReviewScreenshots/{res['id']}", json={
        "data": {"type": "inAppPurchaseAppStoreReviewScreenshots", "id": res["id"],
                 "attributes": {"uploaded": True, "sourceFileChecksum": hashlib.md5(data).hexdigest()}}})


def ios_plan(c: httpx.Client, app_id: str, local: dict, remote: dict | None, folder: Path) -> list:
    """List of (description, action) to make the store match `local`."""
    steps, pid = [], local["product_id"]
    if remote is None:
        if local.get("type") not in IOS_TYPES:
            raise ValueError(f"{pid}: type must be one of {list(IOS_TYPES)}")
        box = {}

        def create():
            box["id"] = call(c, "POST", "/v2/inAppPurchases", json={"data": {
                "type": "inAppPurchases",
                "attributes": compact({"productId": pid, "name": local.get("reference_name") or pid,
                                       "inAppPurchaseType": IOS_TYPES[local["type"]],
                                       "reviewNote": local.get("review_note"),
                                       "familySharable": local.get("family_sharable")}),
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}}}})["data"]["id"]
        steps.append((f"create ({local['type']})", create))
        iap_id = lambda: box["id"]  # noqa: E731
        remote = {"listings": {}, "localization_ids": {}}
        steps.append(("set availability: all territories", lambda: asc_set_availability(c, iap_id())))
    else:
        iap_id = lambda: remote["id"]  # noqa: E731
        changes = {k: local.get(f) for k, f in (("name", "reference_name"), ("reviewNote", "review_note"),
                                                 ("familySharable", "family_sharable"))
                   if f in local and local.get(f) != remote.get(f)}
        if changes:
            steps.append((f"update {pid} {sorted(changes)}", lambda: call(
                c, "PATCH", f"/v2/inAppPurchases/{iap_id()}",
                json={"data": {"type": "inAppPurchases", "id": iap_id(), "attributes": changes}})))

    for loc, l in local.get("listings", {}).items():
        attrs = {"name": l["title"], "description": l.get("description", "")}
        if loc not in remote["listings"]:
            steps.append((f"add listing {loc}", lambda loc=loc, attrs=attrs: call(
                c, "POST", "/v1/inAppPurchaseLocalizations", json={"data": {
                    "type": "inAppPurchaseLocalizations", "attributes": {**attrs, "locale": loc},
                    "relationships": {"inAppPurchaseV2": {"data": {"type": "inAppPurchases", "id": iap_id()}}}}})))
        elif remote["listings"][loc] != {"title": attrs["name"], "description": attrs["description"]}:
            lid = remote["localization_ids"][loc]
            steps.append((f"update listing {loc}", lambda lid=lid, attrs=attrs: call(
                c, "PATCH", f"/v1/inAppPurchaseLocalizations/{lid}",
                json={"data": {"type": "inAppPurchaseLocalizations", "id": lid, "attributes": attrs}})))

    usd = local.get("price_usd")
    if usd and (remote.get("price_usd") is None or Decimal(usd) != Decimal(remote["price_usd"])):
        steps.append((f"price {remote.get('price_usd')} -> {usd} USD",
                      lambda: asc_set_price(c, iap_id(), usd)))
    if local.get("review_screenshot") and not remote.get("has_screenshot"):
        img = folder / local["review_screenshot"]
        steps.append((f"upload review screenshot {img.name}",
                      lambda: asc_upload_screenshot(c, iap_id(), img)))
    return [(f"{pid}: {d}", a) for d, a in steps]


# ---------------------------------------------------------------- Google Play

def play_client(key_path: str, scope: str = "androidpublisher") -> httpx.Client:
    k = json.loads(Path(key_path).read_text())
    now = int(time.time())
    assertion = jwt.encode({"iss": k["client_email"], "scope": f"https://www.googleapis.com/auth/{scope}",
                            "aud": k["token_uri"], "iat": now, "exp": now + 3600},
                           k["private_key"], algorithm="RS256")
    tok = httpx.post(k["token_uri"], data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                           "assertion": assertion}, timeout=30)
    tok.raise_for_status()
    return httpx.Client(headers={"Authorization": f"Bearer {tok.json()['access_token']}"}, timeout=60)


def money(m: dict) -> str:
    return str((Decimal(m.get("units", "0")) + Decimal(m.get("nanos", 0)) / Decimal(10**9)).normalize())


def buy_option(product: dict) -> dict | None:
    opts = [o for o in product.get("purchaseOptions", []) if "buyOption" in o]
    return next((o for o in opts if o["buyOption"].get("legacyCompatible")), opts[0] if opts else None)


def android_remote(c: httpx.Client, package: str) -> dict[str, dict]:
    """productId -> {"raw": play product, **file format}."""
    out, token = {}, ""
    while True:
        r = call(c, "GET", f"{PLAY}/{package}/oneTimeProducts?pageSize=1000&pageToken={token}")
        for p in r.get("oneTimeProducts", []):
            opt = buy_option(p)
            us = next((x["price"] for x in (opt or {}).get("regionalPricingAndAvailabilityConfigs", [])
                       if x["regionCode"] == "US" and "price" in x), None)
            out[p["productId"]] = {
                "raw": p,
                "product_id": p["productId"],
                "price_usd": money(us) if us else None,
                "listings": {l["languageCode"]: {"title": l["title"], "description": l.get("description", "")}
                             for l in p.get("listings", [])},
                "state": (opt or {}).get("state"),
            }
        token = r.get("nextPageToken")
        if not token:
            return out


def play_prices(c: httpx.Client, package: str, usd: str) -> tuple[list, dict, str]:
    d = Decimal(usd)
    r = call(c, "POST", f"{PLAY}/{package}/pricing:convertRegionPrices", json={"price": {
        "currencyCode": "USD", "units": str(int(d)), "nanos": int((d % 1) * 10**9)}})
    configs = [{"regionCode": code, "price": v["price"], "availability": "AVAILABLE"}
               for code, v in r["convertedRegionPrices"].items()]
    other = r["convertedOtherRegionsPrice"]
    new_regions = {"availability": "AVAILABLE", "usdPrice": other["usdPrice"], "eurPrice": other["eurPrice"]}
    return configs, new_regions, r["regionVersion"]["version"]


def android_plan(c: httpx.Client, package: str, local: dict, remote: dict | None) -> list:
    pid = local["product_id"]
    # Play replaces the whole listings field, so merge: languages only on the store are kept.
    merged = {**(remote or {}).get("listings", {}),
              **{k: {"title": v["title"], "description": v.get("description", "")}
                 for k, v in local.get("listings", {}).items()}}
    listings = [{"languageCode": k, **v} for k, v in merged.items()]
    usd = local.get("price_usd")
    price_changed = usd and (remote is None or remote.get("price_usd") is None
                             or Decimal(usd) != Decimal(remote["price_usd"]))
    listings_changed = remote is None or merged != remote["listings"]
    if remote is None and not usd:
        raise ValueError(f"{pid}: price_usd is required to create a product")
    if not (price_changed or listings_changed):
        return []

    def upsert():
        product = dict(remote["raw"]) if remote else {"packageName": package, "productId": pid, "purchaseOptions": [
            {"purchaseOptionId": "default", "buyOption": {"legacyCompatible": True, "multiQuantityEnabled": False}}]}
        product["listings"] = listings
        version = (remote or {}).get("raw", {}).get("regionsVersion", {}).get("version")
        if price_changed:
            # ponytail: regenerates every region from the USD price; custom regional prices get replaced
            configs, new_regions, version = play_prices(c, package, usd)
            opt = buy_option(product)
            opt["regionalPricingAndAvailabilityConfigs"] = configs
            opt["newRegionsConfig"] = new_regions
        for o in product["purchaseOptions"]:
            o.pop("state", None)
        product.pop("regionsVersion", None)
        call(c, "PATCH", f"{PLAY}/{package}/onetimeproducts/{pid}", params={
            "allowMissing": "true", "updateMask": "listings,purchaseOptions", "regionsVersion.version": version},
            json=product)
        if remote is None:
            call(c, "POST", f"{PLAY}/{package}/oneTimeProducts/{pid}/purchaseOptions:batchUpdateStates", json={
                "requests": [{"activatePurchaseOptionRequest": {
                    "packageName": package, "productId": pid, "purchaseOptionId": "default"}}]})

    what = ["create + activate" if remote is None else "update",
            "listings" if listings_changed else "",
            f"price {(remote or {}).get('price_usd')} -> {usd} USD" if price_changed else ""]
    return [(f"{pid}: " + " ".join(w for w in what if w), upsert)]


# ---------------------------------------------------------------- shared

def remote_products(platform: str, key: str, app_id: str):
    """Return (client, store app id, {productId: product})."""
    if platform == "ios":
        c = asc_client(key)
        aid = asc_app_id(c, app_id)
        return c, aid, ios_remote(c, aid)
    c = play_client(key)
    return c, app_id, android_remote(c, app_id)


def to_file(p: dict) -> dict:
    return compact({k: v for k, v in p.items() if k not in ("id", "localization_ids", "raw", "has_screenshot")})


def plan(platform: str, c, store_app: str, local: dict, remote: dict | None, folder: Path) -> list:
    if platform == "ios":
        return ios_plan(c, store_app, local, remote, folder)
    return android_plan(c, store_app, local, remote)
