"""Releases and customer reviews via App Store Connect API and Google Play Developer API."""
import httpx

from iap import PLAY, asc_pages, call


# ---------------------------------------------------------------- Google Play tracks

def play_tracks(c: httpx.Client, package: str) -> list[dict]:
    edit = call(c, "POST", f"{PLAY}/{package}/edits")["id"]
    try:
        return call(c, "GET", f"{PLAY}/{package}/edits/{edit}/tracks").get("tracks", [])
    finally:
        c.delete(f"{PLAY}/{package}/edits/{edit}")


def fmt_release(r: dict) -> str:
    frac = f" {r['userFraction']:.0%}" if "userFraction" in r else ""
    return f"{r.get('name', '?')} codes={','.join(r.get('versionCodes', []))} {r['status']}{frac}"


def android_status(c: httpx.Client, package: str) -> str:
    lines = []
    for t in play_tracks(c, package):
        rels = t.get("releases", [])
        lines.append(f"{t['track']}: " + ("; ".join(fmt_release(r) for r in rels) or "empty"))
    return "\n".join(lines) or "no tracks"


def latest_release(track: dict) -> dict | None:
    rels = [r for r in track.get("releases", []) if r.get("versionCodes")]
    return max(rels, key=lambda r: max(int(v) for v in r["versionCodes"]), default=None)


def android_plan(c: httpx.Client, package: str, action: str, track: str, to_track: str,
                 rollout: float) -> tuple[str, dict, str]:
    """Return (description, release to write, target track)."""
    tracks = {t["track"]: t for t in play_tracks(c, package)}
    if track not in tracks:
        raise ValueError(f"unknown track '{track}', have: {', '.join(tracks)}")
    src = latest_release(tracks[track])
    if not src:
        raise ValueError(f"track '{track}' has no release")
    rel = {k: src[k] for k in ("name", "versionCodes", "releaseNotes", "inAppUpdatePriority") if k in src}

    def staged(r: dict) -> dict:
        if not 0 < rollout <= 1:
            raise ValueError("rollout must be in (0, 1]")
        if rollout == 1:
            return {**r, "status": "completed"}
        return {**r, "status": "inProgress", "userFraction": rollout}

    if action == "promote":
        if not to_track:
            raise ValueError("promote needs to_track")
        new, target = staged(rel), to_track
    elif action == "rollout":
        if src["status"] not in ("inProgress", "halted"):
            raise ValueError(f"latest release on '{track}' is {src['status']}, not a staged rollout")
        new, target = staged(rel), track
    elif action == "halt":
        if src["status"] != "inProgress":
            raise ValueError(f"latest release on '{track}' is {src['status']}, only inProgress can be halted")
        new, target = {**rel, "status": "halted", "userFraction": src["userFraction"]}, track
    else:
        raise ValueError("action must be promote, rollout or halt")
    return f"{target}: {fmt_release(src)} -> {fmt_release(new)}", new, target


def android_apply(c: httpx.Client, package: str, release: dict, track: str):
    edit = call(c, "POST", f"{PLAY}/{package}/edits")["id"]
    try:
        call(c, "PUT", f"{PLAY}/{package}/edits/{edit}/tracks/{track}",
             json={"track": track, "releases": [release]})
        call(c, "POST", f"{PLAY}/{package}/edits/{edit}:commit")
    except Exception:
        c.delete(f"{PLAY}/{package}/edits/{edit}")
        raise


# ---------------------------------------------------------------- App Store Connect versions

def ios_versions(c: httpx.Client, app_id: str) -> list[dict]:
    vs = asc_pages(c, f"/v1/apps/{app_id}/appStoreVersions?filter[platform]=IOS&limit=10"
                      "&include=build,appStoreVersionPhasedRelease")
    return sorted(vs, key=lambda v: v["attributes"].get("createdDate", ""), reverse=True)


def ios_status(c: httpx.Client, app_id: str) -> str:
    lines = ["App Store versions:"]
    for v in ios_versions(c, app_id)[:5]:
        a = v["attributes"]
        lines.append(f"  {a['versionString']}: {a.get('appVersionState') or a.get('appStoreState')}"
                     f" release={a.get('releaseType')}")
    builds = call(c, "GET", f"/v1/builds?filter[app]={app_id}&sort=-uploadedDate&limit=5"
                            "&include=preReleaseVersion")
    pre = {i["id"]: i["attributes"]["version"] for i in builds.get("included", [])}
    lines.append("Latest builds:")
    for b in builds["data"]:
        a = b["attributes"]
        ver = pre.get((b["relationships"]["preReleaseVersion"].get("data") or {}).get("id"), "?")
        lines.append(f"  {ver} ({a['version']}): {a['processingState']} uploaded={a['uploadedDate'][:16]}")
    return "\n".join(lines)


def find_version(c: httpx.Client, app_id: str, version: str) -> dict | None:
    vs = ios_versions(c, app_id)
    if not version:
        return vs[0] if vs else None
    return next((v for v in vs if v["attributes"]["versionString"] == version), None)


def ios_plan(c: httpx.Client, app_id: str, action: str, version: str, build: str) -> list:
    """List of (description, action)."""
    v = find_version(c, app_id, version)
    state = (v or {}).get("attributes", {}).get("appVersionState")
    if action == "submit":
        if not version:
            raise ValueError("submit needs version (e.g. '1.2.0')")
        steps, box = [], {"id": v and v["id"]}
        if not v:
            steps.append((f"create version {version}", lambda: box.update(id=call(
                c, "POST", "/v1/appStoreVersions", json={"data": {
                    "type": "appStoreVersions", "attributes": {"platform": "IOS", "versionString": version},
                    "relationships": {"app": {"data": {"type": "apps", "id": app_id}}}}})["data"]["id"])))
        elif state not in ("PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED"):
            raise ValueError(f"version {version} is {state}, cannot submit")
        q = f"/v1/builds?filter[app]={app_id}&filter[preReleaseVersion.version]={version}" \
            f"&filter[processingState]=VALID&sort=-uploadedDate&limit=1"
        if build:
            q += f"&filter[version]={build}"
        b = call(c, "GET", q)["data"]
        if not b:
            raise ValueError(f"no processed build for {version}" + (f" ({build})" if build else ""))
        bid, bnum = b[0]["id"], b[0]["attributes"]["version"]
        steps.append((f"attach build {bnum} to {version}", lambda: call(
            c, "PATCH", f"/v1/appStoreVersions/{box['id']}/relationships/build",
            json={"data": {"type": "builds", "id": bid}})))
        steps.append((f"submit {version} for App Review", lambda: submit(c, app_id, box["id"])))
        return steps
    if not v:
        raise ValueError("no App Store version found")
    vid, vs = v["id"], v["attributes"]["versionString"]
    if action == "release":
        if state != "PENDING_DEVELOPER_RELEASE":
            raise ValueError(f"version {vs} is {state}, only PENDING_DEVELOPER_RELEASE can be released")
        return [(f"release {vs} to the App Store", lambda: call(
            c, "POST", "/v1/appStoreVersionReleaseRequests", json={"data": {
                "type": "appStoreVersionReleaseRequests",
                "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": vid}}}}}))]
    phase = {"pause": "PAUSED", "resume": "ACTIVE", "complete": "COMPLETE"}.get(action)
    if not phase:
        raise ValueError("action must be submit, release, pause, resume or complete")
    pr = call(c, "GET", f"/v1/appStoreVersions/{vid}/appStoreVersionPhasedRelease").get("data")
    if not pr:
        raise ValueError(f"version {vs} has no phased release")
    return [(f"phased release of {vs}: {pr['attributes']['phasedReleaseState']} -> {phase}", lambda: call(
        c, "PATCH", f"/v1/appStoreVersionPhasedReleases/{pr['id']}", json={"data": {
            "type": "appStoreVersionPhasedReleases", "id": pr["id"],
            "attributes": {"phasedReleaseState": phase}}}))]


def submit(c: httpx.Client, app_id: str, version_id: str):
    open_subs = call(c, "GET", f"/v1/reviewSubmissions?filter[app]={app_id}&filter[platform]=IOS"
                               "&filter[state]=READY_FOR_REVIEW&limit=1")["data"]
    sid = open_subs[0]["id"] if open_subs else call(c, "POST", "/v1/reviewSubmissions", json={"data": {
        "type": "reviewSubmissions", "attributes": {"platform": "IOS"},
        "relationships": {"app": {"data": {"type": "apps", "id": app_id}}}}})["data"]["id"]
    call(c, "POST", "/v1/reviewSubmissionItems", json={"data": {"type": "reviewSubmissionItems", "relationships": {
        "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": sid}},
        "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}}}})
    call(c, "PATCH", f"/v1/reviewSubmissions/{sid}", json={"data": {
        "type": "reviewSubmissions", "id": sid, "attributes": {"submitted": True}}})


# ---------------------------------------------------------------- customer reviews

def ios_reviews(c: httpx.Client, app_id: str, limit: int, unanswered: bool) -> str:
    q = f"/v1/apps/{app_id}/customerReviews?sort=-createdDate&limit={limit}&include=response"
    if unanswered:
        q += "&exists[publishedResponse]=false"
    r = call(c, "GET", q)
    replies = {i["id"]: i["attributes"].get("responseBody") for i in r.get("included", [])}
    out = []
    for rv in r["data"]:
        a = rv["attributes"]
        reply = replies.get(((rv.get("relationships", {}).get("response") or {}).get("data") or {}).get("id"))
        out.append(f"[{rv['id']}] {'★' * a['rating']} {a['territory']} {a['createdDate'][:10]} "
                   f"{a.get('reviewerNickname', '')}\n  {a.get('title', '')}: {a.get('body', '')}"
                   + (f"\n  ↳ reply: {reply}" if reply else ""))
    return "\n".join(out) or "no reviews"


def ios_reply(c: httpx.Client, review_id: str, text: str) -> str:
    call(c, "POST", "/v1/customerReviewResponses", json={"data": {
        "type": "customerReviewResponses", "attributes": {"responseBody": text},
        "relationships": {"review": {"data": {"type": "customerReviews", "id": review_id}}}}})
    return "reply submitted (Apple publishes it after a short review)"


def android_reviews(c: httpx.Client, package: str, limit: int, unanswered: bool) -> str:
    r = call(c, "GET", f"{PLAY}/{package}/reviews?maxResults={min(limit, 100)}")
    out = []
    for rv in r.get("reviews", []):
        user = next((x["userComment"] for x in rv["comments"] if "userComment" in x), {})
        dev = next((x["developerComment"] for x in rv["comments"] if "developerComment" in x), None)
        if unanswered and dev:
            continue
        out.append(f"[{rv['reviewId']}] {'★' * user.get('starRating', 0)} {user.get('reviewerLanguage', '')} "
                   f"v{user.get('appVersionName', '?')} {rv.get('authorName', '')}\n  {user.get('text', '').strip()}"
                   + (f"\n  ↳ reply: {dev['text']}" if dev else ""))
    return "\n".join(out) or "no reviews (Play API only returns reviews from the last 7 days)"


def android_reply(c: httpx.Client, package: str, review_id: str, text: str) -> str:
    call(c, "POST", f"{PLAY}/{package}/reviews/{review_id}:reply", json={"replyText": text})
    return "reply posted"
