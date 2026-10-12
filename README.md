# store-mcp

An MCP server for managing App Store and Google Play store listings, backed by
[fastlane](https://fastlane.tools) (`deliver` for iOS, `supply` for Android).

Listings are stored as plain files. Pull them, edit the text, review the diff, then push.

## Tools

| Tool | What it does |
|---|---|
| `store_apps` | List configured apps, their bundle ID / package name, and account |
| `store_pull(app, platform, screenshots=False, overwrite=False)` | Download the live listing to local files. Refuses to overwrite existing local files unless `overwrite=True` |
| `store_diff(app, platform)` | Diff the live listing (`---`) against local files (`+++`) |
| `store_push(app, platform, dry_run=True, screenshots=False, app_version="")` | Upload local files to the store |
| `iap_pull(app, platform, overwrite=False)` | Download one-time in-app purchases to JSON files |
| `iap_push(app, platform, product_id="", dry_run=True)` | Create or update in-app purchases from JSON files |
| `build_upload(app, platform, file, track="internal", ...)` | Upload an .aab/.apk to a testing track or an .ipa to TestFlight (never production) |
| `release_status(app, platform)` | Play tracks and releases, or App Store versions and latest builds |
| `android_release(app, action, track, to_track="", rollout=1.0, dry_run=True)` | `promote` a release to another track, change staged `rollout`, or `halt` it |
| `ios_release(app, action, version="", build="", dry_run=True)` | `submit` a version for review, `release` an approved version, `pause` / `resume` / `complete` a phased release |
| `reviews_list(app, platform, limit=20, unanswered=False)` | Latest customer reviews (Play API only returns the last 7 days) |
| `review_reply(app, platform, review_id, text)` | Publicly reply to a review |
| `vitals(app, platform)` | Android: 28-day crash/ANR rates and top issues. iOS: Xcode metrics and diagnostic signatures |
| `testers(app, platform, action="list", group="", emails="")` | TestFlight groups and testers; Play closed-track Google Groups |
| `subscriptions_list(app, platform)` | Subscriptions with period, US price and state (read-only) |
| `orders(app, order_id="", voided_days=30)` | Google Play order details, or refunded/voided purchases |
| `ads_accounts()` | Google Ads accounts under the configured manager (MCC) account |
| `ads_query(gaql, customer_id="")` | Run any GAQL query: campaigns, ad groups, ads, metrics (money in micros) |
| `ads_mutate(operations, customer_id="", dry_run=True, allow_enable=False)` | Create / edit / remove campaigns, budgets, ad groups, ads atomically. New campaigns are forced to `PAUSED`; enabling a campaign needs `allow_enable=True` |
| `admob_inventory(publisher_id="")` | AdMob account, apps and their ad units |
| `admob_report(start, end, metrics, dimensions=["DATE"], kind="network", filters={}, sort="", limit=200)` | Network, mediation or campaign report |
| `admob_ui_open / _click / _fill / _page / _screenshot / _close` | Drive the AdMob web UI in a headless Camoufox (ad units, mediation groups, anything without API access). Commit buttons need `confirm=True` |

`platform` is `ios` or `android`.

`store_push` safety:
- `dry_run=True` by default. Android runs Play's `validate_only`; iOS returns the diff.
- Never uploads binaries and never submits for review.
- iOS without `app_version` edits the current editable version and never creates a new one.

Release safety: `android_release` and `ios_release` are dry runs by default, and `build_upload`
refuses the Play production track. Production goes through `android_release promote`
or `ios_release submit`.

`iap_push` safety:
- `dry_run=True` by default and lists the planned changes.
- Never deletes products or listings.
- Changing `price_usd` resets every region from the USD price (Apple equalization / Play conversion).

## In-app purchases

Consumable and non-consumable products, one shared format for both stores
(App Store Connect API and Play `monetization.onetimeproducts`, no fastlane):

```json
{
  "product_id": "coins_500",
  "type": "consumable",
  "reference_name": "500 Coins",
  "price_usd": "2.99",
  "listings": {"en-US": {"title": "500 Coins", "description": "A stack of coins."}},
  "review_note": "Open Store > Coins.",
  "review_screenshot": "coins_500.png",
  "family_sharable": false
}
```

`type`, `reference_name`, `review_note`, `review_screenshot` (path relative to the JSON file)
and `family_sharable` are iOS only. `state` is read-only. To create a product, add a new file and push.
New iOS products need a review screenshot and are submitted for review together with the next app version.

## Local layout

```
<root>/<app>/android/<locale>/title.txt, short_description.txt, full_description.txt, images/...
<root>/<app>/ios/metadata/<locale>/name.txt, subtitle.txt, description.txt, keywords.txt, ...
<root>/<app>/ios/screenshots/...
<root>/<app>/iap/{ios,android}/<product_id>.json
```

## Setup

```bash
brew install fastlane uv
git clone git@github.com:nthienquang199x/store-mcp.git && cd store-mcp
mkdir -p ~/.config/store-mcp && cp apps.example.toml ~/.config/store-mcp/apps.toml   # then edit
claude mcp add -s user store -- uv run --frozen --directory "$PWD" store-mcp
```

Set `STORE_CONFIG` to use a config file somewhere else.

### Credentials

- **Google Play:** a service account JSON key with access to the app in Play Console.
- **App Store Connect:** create an API key (App Store Connect > Users and Access > Integrations,
  App Manager role) and save it as fastlane `api_key_path` JSON:

  ```json
  {"key_id": "...", "issuer_id": "...", "key": "-----BEGIN PRIVATE KEY-----\n...", "in_house": false}
  ```

Keys are read from the config only and are never passed as tool arguments.

Extra Google setup for some tools:
- `vitals` (Android): enable the **Google Play Developer Reporting API** in the service account's Cloud project.
- `orders` (Android): grant the service account **View financial data, orders, and cancellation survey responses** in Play Console.

### Multiple accounts

```toml
root = "~/store"

# Defaults, used when an app has no account
play_json_key = "~/.config/store-mcp/play.json"
asc_key = "~/.config/store-mcp/asc.json"

[accounts.client_a]
play_json_key = "~/.config/store-mcp/client_a/play.json"
asc_key = "~/.config/store-mcp/client_a/asc.json"

[apps.myapp]
ios = "com.example.myapp"
android = "com.example.myapp"

[apps.clientapp]
account = "client_a"
ios = "com.clienta.app"
android = "com.clienta.app"
```

Key lookup order: app-level key, then the app's account, then the top-level default.

### Google Ads

1. **Developer token**: in a manager (MCC) account, Admin → API Center.
2. **Production access**: in the Google Cloud project that owns your OAuth client, open the Google Ads
   API page → *Upgrade access level* and apply for Explorer access (granted within minutes). Until
   then calls fail with `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION`.
3. **Credentials**: service accounts get `NOT_ADS_USER`, so use your own login: run `store-mcp-login`
   (see AdMob below), which carries the Google Ads scope, and point `credentials` at its file.
4. Add to `apps.toml`:
   ```toml
   [ads]
   developer_token = "..."
   login_customer_id = "123-456-7890"   # MCC id, when the account sits under a manager
   customer_id = "234-567-8901"         # default account for ads_query / ads_mutate
   credentials = "~/.config/store-mcp/google_oauth.json"   # defaults to play_json_key
   # api_version = "v25"
   ```
   A new ads account answers `CUSTOMER_NOT_ENABLED` until its first campaign and billing are set up.

### AdMob

AdMob does not accept service accounts, so it uses your own Google login.

1. Enable the *AdMob API* in a Google Cloud project, configure the OAuth consent screen (External,
   add yourself as a test user; publish it, or refresh tokens expire after 7 days) and create an
   OAuth client of type *Desktop app*. Download its JSON.
2. Sign in (opens a browser, saves `~/.config/store-mcp/google_oauth.json`):
   ```sh
   uv run --directory /path/to/store-mcp store-mcp-login ~/Downloads/client_secret_....json
   ```
   The same login also carries the Google Ads scope, so `[ads].credentials` may point to this file.
3. Optional, in `apps.toml`:
   ```toml
   [admob]
   publisher_id = "pub-1234567890123456"   # default: the login's first account
   # credentials = "~/.config/store-mcp/google_oauth.json"
   ```

### AdMob web UI (Camoufox)

AdMob's mediation and write APIs are allowlisted per account, so changes go through the web UI in a
headless [Camoufox](https://camoufox.com) with a fixed fingerprint and a persistent profile
(`~/.config/store-mcp/browser`).

```sh
uv run --directory /path/to/store-mcp camoufox fetch                 # once, downloads the browser
uv run --directory /path/to/store-mcp store-mcp-browser-login        # sign in to Google, close the window
```

Run the login again whenever a tool reports that the session expired (call `admob_ui_close` first).

## Not supported yet

Creating or editing subscriptions, iOS order lookup (needs an App Store Server API key),
crash counts on iOS (not in the App Store Connect API).
