"""Store MCP: pull / diff / push App Store + Google Play listings via fastlane.

Listings live as files (<root>/<app>/{ios,android}); edit them, then push.
"""
import os
import shutil
import subprocess
import tempfile
import json
import logging
import tomllib
from pathlib import Path

from mcp.server.mcpserver import MCPServer

import iap
import insights
import release

mcp = MCPServer("store")
logging.getLogger("httpx").setLevel(logging.WARNING)
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


@mcp.tool()
def store_apps() -> str:
    """List configured apps and their iOS bundle id / Android package name."""
    c = cfg()
    return "\n".join(f"{name}: ios={a.get('ios', '-')} android={a.get('android', '-')} account={a.get('account', 'default')}"
                     for name, a in c.get("apps", {}).items()) or f"no apps in {CONFIG}"


@mcp.tool()
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


@mcp.tool()
def store_diff(app: str, platform: str) -> str:
    """Unified diff: live store listing (---) vs local files (+++). Text only for iOS."""
    c, app_id, local = resolve(app, platform)
    with tempfile.TemporaryDirectory() as tmp:
        remote = Path(tmp) / "remote"
        download(c, platform, app_id, remote, screenshots=False)
        if platform == "ios":
            return diff(remote / "metadata", local / "metadata")
        return diff(remote, local)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def release_status(app: str, platform: str) -> str:
    """Android: every track with its releases (version codes, status, rollout %).
    iOS: recent App Store versions with their state, and the latest builds."""
    c, app_id = api(app, platform)
    return release.ios_status(c, app_id) if platform == "ios" else release.android_status(c, app_id)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def reviews_list(app: str, platform: str, limit: int = 20, unanswered: bool = False) -> str:
    """Latest customer reviews with their id, rating, text and existing reply.
    Google Play only returns reviews from the last 7 days."""
    c, app_id = api(app, platform)
    if platform == "ios":
        return release.ios_reviews(c, app_id, limit, unanswered)
    return release.android_reviews(c, app_id, limit, unanswered)


@mcp.tool()
def review_reply(app: str, platform: str, review_id: str, text: str) -> str:
    """Publicly reply to a customer review (replaces an existing reply on Google Play)."""
    c, app_id = api(app, platform)
    if platform == "ios":
        return release.ios_reply(c, review_id, text)
    return release.android_reply(c, app_id, review_id, text)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def subscriptions_list(app: str, platform: str) -> str:
    """Read-only list of auto-renewing subscriptions: product id, period, US price, state."""
    c, app_id = api(app, platform)
    return insights.ios_subscriptions(c, app_id) if platform == "ios" else insights.android_subscriptions(c, app_id)


@mcp.tool()
def orders(app: str, order_id: str = "", voided_days: int = 30) -> str:
    """Google Play orders. With order_id (GPA.xxxx): order details. Without: refunded / voided
    purchases from the last `voided_days` days (max 30). iOS orders are not supported yet."""
    c, package = api(app, "android")
    return insights.android_orders(c, package, order_id, voided_days)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
