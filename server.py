"""Store MCP: pull / diff / push App Store + Google Play listings via fastlane.

Listings live as files (<root>/<app>/{ios,android}); edit them, then push.
"""
import functools
import inspect
import os
import shutil
import subprocess
import tempfile
import json
import logging
import tomllib
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp.types import ToolAnnotations

import admob
import ads
import browser
import iap
import insights
import release

mcp = MCPServer("store")
logging.getLogger("httpx").setLevel(logging.WARNING)


def tool(**kw):
    """mcp.tool that passes failures on to the model: the SDK hides any error that isn't a ToolError."""
    def wrap(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def run(*a, **k):
                try:
                    return await fn(*a, **k)
                except ToolError:
                    raise
                except Exception as e:
                    raise ToolError(f"{type(e).__name__}: {e}") from e
        else:
            @functools.wraps(fn)
            def run(*a, **k):
                try:
                    return fn(*a, **k)
                except ToolError:
                    raise
                except Exception as e:
                    raise ToolError(f"{type(e).__name__}: {e}") from e
        return mcp.tool(**kw)(run)
    return wrap
CONFIG = Path(os.environ.get("STORE_CONFIG", "~/.config/store-mcp/apps.toml")).expanduser()
ENV = {
    **os.environ,
    "LC_ALL": "en_US.UTF-8",
    "LANG": "en_US.UTF-8",
    "CI": "1",  # never prompt
    "FASTLANE_SKIP_UPDATE_CHECK": "1",
    "FASTLANE_HIDE_CHANGELOG": "1",
    "FASTLANE_DISABLE_COLORS": "1",
}


def cfg() -> dict:
    return tomllib.loads(CONFIG.read_text())


def path(value: str) -> str:
    return str(Path(value).expanduser())


def resolve(app: str, platform: str) -> tuple[dict, str, Path]:
    """Return (config, bundle/package id, local dir) or raise with a useful message."""
    if platform not in ("ios", "android"):
        raise ValueError("platform must be 'ios' or 'android'")
    c = cfg()
    apps = c.get("apps", {})
    if app not in apps:
        raise ValueError(f"unknown app '{app}', configured: {', '.join(apps) or 'none'}")
    a = apps[app]
    app_id = a.get(platform)
    if not app_id:
        raise ValueError(f"app '{app}' has no {platform} id in {CONFIG}")
    root = Path(path(c.get("root", "~/store"))) / app / platform
    # Keys: app-level > its [accounts.<name>] > top-level default.
    account = c.get("accounts", {}).get(a.get("account"), {})
    if a.get("account") and not account:
        raise ValueError(f"app '{app}' uses unknown account '{a['account']}'")
    keys = {k: a.get(k) or account.get(k) or c.get(k) for k in ("play_json_key", "asc_key")}
    key = "asc_key" if platform == "ios" else "play_json_key"
    if not keys[key]:
        raise ValueError(f"no {key} for app '{app}' (set it on the app, its account, or top-level)")
    return keys, app_id, root


def run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, env=ENV, timeout=1800)
    out = (r.stdout + r.stderr).strip()
    # ponytail: tail-truncated log, return a log file path if full logs are ever needed
    out = out[-8000:]
    if r.returncode:
        raise RuntimeError(f"exit {r.returncode}: {' '.join(cmd[:3])}\n{out}")
    return out


def download(c: dict, platform: str, app_id: str, dest: Path, screenshots: bool) -> str:
    """Download the live store listing into an empty dir `dest`."""
    if platform == "android":
        # supply downloads text + images into <dest>/<locale>/...
        return run(["fastlane", "supply", "init", "--package_name", app_id,
                    "--json_key", path(c["play_json_key"]), "--metadata_path", str(dest)])
    key = path(c["asc_key"])
    log = run(["fastlane", "deliver", "download_metadata", "--api_key_path", key,
               "-a", app_id, "-m", str(dest / "metadata"), "--force"])
    if screenshots:
        log += "\n" + run(["fastlane", "deliver", "download_screenshots", "--api_key_path", key,
                           "-a", app_id, "-w", str(dest / "screenshots")])
    return log


def diff(a: Path, b: Path) -> str:
    r = subprocess.run(["diff", "-ruN", "--exclude=.DS_Store", str(a), str(b)],
                       capture_output=True, text=True)
    return r.stdout[-20000:] or "no differences"


@tool()
def store_apps() -> str:
    """List configured apps and their iOS bundle id / Android package name."""
    c = cfg()
    return "\n".join(f"{name}: ios={a.get('ios', '-')} android={a.get('android', '-')} account={a.get('account', 'default')}"
                     for name, a in c.get("apps", {}).items()) or f"no apps in {CONFIG}"


@tool()
def store_pull(app: str, platform: str, screenshots: bool = False, overwrite: bool = False) -> str:
    """Download the current store listing to local files and return the local path.

    platform: 'ios' or 'android'. Android always includes images; iOS screenshots only
    when screenshots=True. Refuses to replace existing local files unless overwrite=True
    (run store_diff first so local edits aren't lost).
    """
    c, app_id, local = resolve(app, platform)
    if local.exists() and any(local.iterdir()) and not overwrite:
        return f"{local} already has files. Run store_diff, or pull with overwrite=True."
    with tempfile.TemporaryDirectory() as tmp:
        log = download(c, platform, app_id, Path(tmp) / "x", screenshots)
        shutil.rmtree(local, ignore_errors=True)
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(Path(tmp) / "x", local)
    return f"pulled to {local}\n\n{log[-2000:]}"


@tool()
def store_diff(app: str, platform: str) -> str:
    """Unified diff: live store listing (---) vs local files (+++). Text only for iOS."""
    c, app_id, local = resolve(app, platform)
    with tempfile.TemporaryDirectory() as tmp:
        remote = Path(tmp) / "remote"
        download(c, platform, app_id, remote, screenshots=False)
        if platform == "ios":
            return diff(remote / "metadata", local / "metadata")
        return diff(remote, local)


@tool()
def store_push(app: str, platform: str, dry_run: bool = True, screenshots: bool = False,
               app_version: str = "") -> str:
    """Upload local listing files to the store. Never uploads binaries or submits for review.

    dry_run=True (default): android runs Play's validate_only; ios returns store_diff.
    screenshots=True also uploads images/screenshots.
    app_version (ios): version to edit/create; empty = current editable version, never creates one.
    """
    c, app_id, local = resolve(app, platform)
    if not local.exists():
        return f"{local} missing, run store_pull first."
    if platform == "android":
        skip_images = "false" if screenshots else "true"
        return run(["fastlane", "supply", "--package_name", app_id,
                    "--json_key", path(c["play_json_key"]), "--metadata_path", str(local),
                    "--skip_upload_apk", "true", "--skip_upload_aab", "true",
                    "--skip_upload_changelogs", "true",
                    "--skip_upload_images", skip_images, "--skip_upload_screenshots", skip_images,
                    "--validate_only", str(dry_run).lower()])
    if dry_run:
        return "dry run (nothing uploaded). Changes that would be pushed:\n\n" + store_diff(app, platform)
    cmd = ["fastlane", "deliver", "--api_key_path", path(c["asc_key"]), "-a", app_id,
           "-m", str(local / "metadata"), "--skip_binary_upload", "true",
           "--submit_for_review", "false", "--run_precheck_before_submit", "false",
           "--force", "true"]
    cmd += ["--app_version", app_version] if app_version else ["--skip_app_version_update", "true"]
    cmd += ["-w", str(local / "screenshots"), "--overwrite_screenshots", "true"] if screenshots \
        else ["--skip_screenshots", "true"]
    return run(cmd)


def iap_setup(app: str, platform: str):
    c, app_id, local = resolve(app, platform)
    key = c["asc_key" if platform == "ios" else "play_json_key"]
    return (*iap.remote_products(platform, path(key), app_id), local.parent / "iap" / platform)


@tool()
def iap_pull(app: str, platform: str, overwrite: bool = False) -> str:
    """Download one-time in-app purchases (consumable / non-consumable) to
    <root>/<app>/iap/<platform>/<product_id>.json. Skips files that exist unless overwrite=True.
    Subscriptions are not included."""
    _, _, remote, folder = iap_setup(app, platform)
    folder.mkdir(parents=True, exist_ok=True)
    written, skipped = [], []
    for pid, p in remote.items():
        f = folder / f"{pid}.json"
        if f.exists() and not overwrite:
            skipped.append(pid)
            continue
        f.write_text(json.dumps(iap.to_file(p), indent=2, ensure_ascii=False) + "\n")
        written.append(pid)
    return (f"{folder}\nwritten: {', '.join(written) or 'none'}"
            + (f"\nskipped (exists, use overwrite=True): {', '.join(skipped)}" if skipped else ""))


@tool()
def iap_push(app: str, platform: str, product_id: str = "", dry_run: bool = True) -> str:
    """Create or update one-time in-app purchases from local json files (all, or one product_id).

    File format: see iap_pull output, or the iap.py docstring. To create a product, write a new
    <product_id>.json with product_id, type, reference_name, price_usd and listings.
    dry_run=True (default) only lists the planned changes. Never deletes products or listings.
    Price changes reset all regions from price_usd (Apple / Play conversion).
    iOS: new products need a review_screenshot and are submitted with the next app version.
    """
    c, store_app, remote, folder = iap_setup(app, platform)
    files = [folder / f"{product_id}.json"] if product_id else sorted(folder.glob("*.json"))
    if not files or not files[0].exists():
        return f"no product files in {folder}"
    steps = []
    for f in files:
        local = json.loads(f.read_text())
        steps += iap.plan(platform, c, store_app, local, remote.get(local["product_id"]), folder)
    if not steps:
        return "store already matches local files"
    return run_steps(steps, dry_run)


def api(app: str, platform: str):
    """Return (http client, store app id: ASC numeric id or Play package)."""
    c, app_id, _ = resolve(app, platform)
    if platform == "ios":
        client = iap.asc_client(path(c["asc_key"]))
        return client, iap.asc_app_id(client, app_id)
    return iap.play_client(path(c["play_json_key"])), app_id


def run_steps(steps: list, dry_run: bool) -> str:
    if dry_run:
        return "dry run, planned:\n" + "\n".join(f"- {d}" for d, _ in steps)
    done = []
    for d, action in steps:
        try:
            action()
        except Exception as e:
            return "\n".join([*(f"done: {x}" for x in done), f"FAILED: {d}\n{e}"])
        done.append(d)
    return "\n".join(f"done: {x}" for x in done)


@tool()
def build_upload(app: str, platform: str, file: str, track: str = "internal",
                 release_status: str = "completed", changelog: str = "", groups: str = "") -> str:
    """Upload a build to testing. Never goes to production directly (use android_release / ios_release).

    android: .aab/.apk to `track` (internal, alpha, beta or a custom closed track).
      release_status: completed, or draft for apps that were never published.
    ios: .ipa to TestFlight. changelog = "What to Test". groups = comma-separated TestFlight
      groups to distribute to after processing (waits for processing).
    """
    c, app_id, _ = resolve(app, platform)
    f = path(file)
    if not Path(f).is_file():
        return f"file not found: {f}"
    if platform == "android":
        if track == "production":
            return "refusing to upload straight to production; upload to a testing track, then promote"
        return run(["fastlane", "supply", "--package_name", app_id, "--json_key", path(c["play_json_key"]),
                    "--aab" if f.endswith(".aab") else "--apk", f, "--track", track,
                    "--release_status", release_status, "--skip_upload_metadata", "true",
                    "--skip_upload_images", "true", "--skip_upload_screenshots", "true",
                    "--skip_upload_changelogs", "true"])
    cmd = ["fastlane", "pilot", "upload", "--api_key_path", path(c["asc_key"]), "-a", app_id, "--ipa", f]
    if changelog:
        cmd += ["--changelog", changelog]
    cmd += ["--groups", groups, "--distribute_external", "false"] if groups \
        else ["--skip_waiting_for_build_processing", "true", "--skip_submission", "true"]
    return run(cmd)


@tool()
def release_status(app: str, platform: str) -> str:
    """Android: every track with its releases (version codes, status, rollout %).
    iOS: recent App Store versions with their state, and the latest builds."""
    c, app_id = api(app, platform)
    return release.ios_status(c, app_id) if platform == "ios" else release.android_status(c, app_id)


@tool()
def android_release(app: str, action: str, track: str, to_track: str = "", rollout: float = 1.0,
                    dry_run: bool = True) -> str:
    """Change a Google Play release. dry_run=True (default) only shows the change.

    action:
      promote: copy the latest release of `track` to `to_track` (rollout < 1 = staged rollout)
      rollout: set the staged rollout of `track` to `rollout` (1.0 completes it); resumes a halted one
      halt:    halt the staged rollout on `track`
    """
    c, package = api(app, "android")
    desc, rel, target = release.android_plan(c, package, action, track, to_track, rollout)
    return run_steps([(desc, lambda: release.android_apply(c, package, rel, target))], dry_run)


@tool()
def ios_release(app: str, action: str, version: str = "", build: str = "", dry_run: bool = True) -> str:
    """Change an App Store release. dry_run=True (default) only shows the steps.

    action:
      submit:   create `version` if missing, attach the latest processed build (or `build` number),
                and submit for App Review. Metadata/screenshots must already be complete.
      release:  release an approved version that is waiting for manual release
      pause | resume | complete: control the phased release
    version defaults to the newest App Store version (required for submit).
    """
    c, app_id = api(app, "ios")
    return run_steps(release.ios_plan(c, app_id, action, version, build), dry_run)


@tool()
def reviews_list(app: str, platform: str, limit: int = 20, unanswered: bool = False) -> str:
    """Latest customer reviews with their id, rating, text and existing reply.
    Google Play only returns reviews from the last 7 days."""
    c, app_id = api(app, platform)
    if platform == "ios":
        return release.ios_reviews(c, app_id, limit, unanswered)
    return release.android_reviews(c, app_id, limit, unanswered)


@tool()
def review_reply(app: str, platform: str, review_id: str, text: str) -> str:
    """Publicly reply to a customer review (replaces an existing reply on Google Play)."""
    c, app_id = api(app, platform)
    if platform == "ios":
        return release.ios_reply(c, review_id, text)
    return release.android_reply(c, app_id, review_id, text)


@tool()
def vitals(app: str, platform: str) -> str:
    """App health. android: 28-day crash and ANR rates plus top crash/ANR issues
    (needs the Google Play Developer Reporting API enabled for the service account's project).
    ios: Xcode metrics (launch time, hangs, memory, battery, disk) and the latest build's
    diagnostic signatures. Apple has no crash counts in its API."""
    c, app_id = api(app, platform)
    if platform == "ios":
        return insights.ios_vitals(c, app_id)
    conf, package, _ = resolve(app, platform)
    return insights.android_vitals(iap.play_client(path(conf["play_json_key"]), "playdeveloperreporting"), package)


@tool()
def testers(app: str, platform: str, action: str = "list", group: str = "", emails: str = "") -> str:
    """Manage testers. action: list | add | remove. emails: comma-separated.
    ios: TestFlight. list without group = all groups with counts; group = TestFlight group name.
    android: group = closed-testing track name; emails are Google Group addresses
    (the Play API cannot manage individual tester emails or internal-testing lists)."""
    c, app_id = api(app, platform)
    mails = [e.strip() for e in emails.split(",") if e.strip()]
    if action != "list" and not mails:
        return "emails required"
    if platform == "ios":
        return insights.ios_testers(c, app_id, action, group, mails)
    return insights.android_testers(c, app_id, action, group, mails)


@tool()
def subscriptions_list(app: str, platform: str) -> str:
    """Read-only list of auto-renewing subscriptions: product id, period, US price, state."""
    c, app_id = api(app, platform)
    return insights.ios_subscriptions(c, app_id) if platform == "ios" else insights.android_subscriptions(c, app_id)


@tool()
def orders(app: str, order_id: str = "", voided_days: int = 30) -> str:
    """Google Play orders. With order_id (GPA.xxxx): order details. Without: refunded / voided
    purchases from the last `voided_days` days (max 30). iOS orders are not supported yet."""
    c, package = api(app, "android")
    return insights.android_orders(c, package, order_id, voided_days)


# ---------------------------------------------------------------- Google Ads

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(destructive_hint=True, open_world_hint=True)


def ads_api(customer_id: str = ""):
    c = cfg()
    a = c.get("ads", {})
    if not a.get("developer_token"):
        raise ValueError(f"add an [ads] section with developer_token to {CONFIG}")
    key = a.get("credentials") or c.get("play_json_key")
    if not key:
        raise ValueError("set [ads].credentials (or top-level play_json_key) to a service account JSON")
    c, base = ads.client(a, key)
    return c, base, a, ads.digits(customer_id or a.get("customer_id", ""))


@tool(annotations=READ)
def ads_accounts() -> str:
    """Google Ads accounts reachable with the configured credentials (MCC children if login_customer_id is set)."""
    c, base, a, _ = ads_api()
    return ads.accounts(c, base, a)


@tool(annotations=READ)
def ads_query(gaql: str, customer_id: str = "") -> str:
    """Run a Google Ads Query Language (GAQL) query. customer_id defaults to [ads].customer_id.
    Money fields are in micros (1_000_000 = 1 unit of account currency). Rows include resourceName,
    which ads_mutate update/remove operations need. Example:
    SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type,
      campaign_budget.amount_micros, metrics.impressions, metrics.clicks, metrics.cost_micros,
      metrics.conversions FROM campaign WHERE segments.date DURING LAST_30_DAYS"""
    c, base, _, cid = ads_api(customer_id)
    if not cid:
        return "customer_id required (or set [ads].customer_id)"
    return ads.query(c, base, cid, gaql)


@tool(annotations=WRITE)
def ads_mutate(operations: list[dict], customer_id: str = "", dry_run: bool = True,
               allow_enable: bool = False) -> str:
    """Create / update / remove Google Ads resources atomically via GoogleAdsService.Mutate (REST JSON,
    camelCase). operations = list of MutateOperation, e.g.
      {"campaignBudgetOperation": {"create": {"resourceName": "customers/CID/campaignBudgets/-1",
          "name": "Echoes budget", "amountMicros": "5000000", "deliveryMethod": "STANDARD",
          "explicitlyShared": false}}}
      {"campaignOperation": {"create": {"name": "Echoes Android installs",
          "advertisingChannelType": "MULTI_CHANNEL", "advertisingChannelSubType": "APP_CAMPAIGN",
          "campaignBudget": "customers/CID/campaignBudgets/-1",
          "appCampaignSetting": {"appId": "com.gamma.echoes", "appStore": "GOOGLE_APP_STORE",
              "biddingStrategyGoalType": "OPTIMIZE_INSTALLS_TARGET_INSTALL_COST"},
          "targetCpa": {"targetCpaMicros": "500000"},
          "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"}}}
      {"campaignOperation": {"update": {"resourceName": "customers/CID/campaigns/123", "status": "PAUSED"},
          "updateMask": "status"}}
    Negative ids (-1, -2) are temp ids to link resources created in the same call. App campaigns then
    need an adGroupOperation and an adGroupAdOperation with appAd {headlines, descriptions, images}.
    dry_run=true validates only (validateOnly) — always dry-run first and show the user.
    New campaigns are forced to PAUSED; setting a campaign to ENABLED needs allow_enable=true,
    which must only be passed after the user explicitly approves spending."""
    c, base, _, cid = ads_api(customer_id)
    if not cid:
        return "customer_id required (or set [ads].customer_id)"
    return ads.mutate(c, base, cid, operations, dry_run, allow_enable)


# ---------------------------------------------------------------- AdMob

def admob_api(publisher_id: str = ""):
    a = cfg().get("admob", {})
    key = Path(path(a.get("credentials", "~/.config/store-mcp/google_oauth.json")))
    if not key.exists():
        raise ValueError(f"no AdMob login at {key}; run: store-mcp-login <oauth-desktop-client.json>")
    c = admob.client(str(key))
    return c, admob.account(c, publisher_id or a.get("publisher_id", ""))


@tool(annotations=READ)
def admob_inventory(publisher_id: str = "") -> str:
    """AdMob account (currency, time zone), its apps (app id, platform, store id, approval state) and each
    app's ad units (ad unit id, format, name). publisher_id defaults to [admob].publisher_id or the login's account."""
    c, parent = admob_api(publisher_id)
    return admob.inventory(c, parent)


@tool(annotations=READ)
def admob_report(start: str, end: str, metrics: list[str], dimensions: list[str] = ["DATE"],
                 kind: str = "network", filters: dict[str, list[str]] = {}, sort: str = "",
                 limit: int = 200, publisher_id: str = "") -> str:
    """AdMob report. kind: network (AdMob network earnings) | mediation (per ad source) | campaign
    (AdMob app campaigns, v1beta, max 30 days). Dates YYYY-MM-DD inclusive, in the account time zone.
    filters: {dimension: [values]}, e.g. {"APP": ["ca-app-pub-xxx~yyy"]} or {"COUNTRY": ["US"]}.
    sort: "<metric or dimension> desc|asc". Money metrics are returned in account currency units.
    network dims: DATE MONTH WEEK AD_UNIT APP AD_TYPE COUNTRY FORMAT PLATFORM MOBILE_OS_VERSION
      GMA_SDK_VERSION APP_VERSION_NAME SERVING_RESTRICTION
    network metrics: AD_REQUESTS CLICKS ESTIMATED_EARNINGS IMPRESSIONS IMPRESSION_CTR IMPRESSION_RPM
      MATCHED_REQUESTS MATCH_RATE SHOW_RATE
    mediation adds dims AD_SOURCE AD_SOURCE_INSTANCE MEDIATION_GROUP and metric OBSERVED_ECPM
      (no IMPRESSION_RPM / SHOW_RATE)."""
    c, parent = admob_api(publisher_id)
    return admob.report(c, parent, kind, start, end, dimensions, metrics, filters, sort, limit)


# AdMob's mediation and write APIs are allowlisted per account, so changes go through the web UI
# in a headless Camoufox that reuses the session from store-mcp-browser-login.
UI = browser.Browser()


@tool(annotations=WRITE)
async def admob_ui_open(path: str = "/v2/home") -> str:
    """Open an AdMob web page headless and return its accessibility tree (roles, names, text).
    path examples: /v2/home, /v2/apps, /v2/mediation/groups. Full https://admob.google.com URLs also work.
    Workflow: open -> read tree -> admob_ui_fill / admob_ui_click with Playwright selectors built from the
    tree, e.g. 'role=button[name="Add ad unit"]', 'role=textbox[name="Ad unit name"]', 'text=Rewarded'.
    Prefer reading data with admob_inventory / admob_report; use the UI only for changes."""
    url = path if path.startswith("https://") else f"https://admob.google.com{path}"
    return await UI.open(url)


@tool(annotations=WRITE)
async def admob_ui_click(selector: str, confirm: bool = False) -> str:
    """Click an element (Playwright selector) and return the new accessibility tree. Buttons that commit
    a change (Save, Create, Done, Delete, Enable...) are refused unless confirm=true: first take
    admob_ui_screenshot, show the user exactly what will be saved, and confirm only after they approve."""
    return await UI.click(selector, confirm)


@tool(annotations=WRITE)
async def admob_ui_fill(selector: str, value: str) -> str:
    """Type into a text field, or pick an option label in a <select>. Returns the new accessibility tree."""
    return await UI.fill(selector, value)


@tool(annotations=READ)
async def admob_ui_screenshot() -> Image:
    """Screenshot of the current AdMob page (show it to the user before confirming a change)."""
    return Image(data=await UI.screenshot(), format="png")


@tool(annotations=READ)
async def admob_ui_page() -> str:
    """Accessibility tree of the current page without acting (e.g. after a dialog animates in)."""
    return await UI.snapshot()


@tool(annotations=READ)
async def admob_ui_close() -> str:
    """Close the headless browser (needed before running store-mcp-browser-login)."""
    await UI.close()
    return "closed"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
