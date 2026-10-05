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

`platform` is `ios` or `android`.

`store_push` safety:
- `dry_run=True` by default. Android runs Play's `validate_only`; iOS returns the diff.
- Never uploads binaries and never submits for review.
- iOS without `app_version` edits the current editable version and never creates a new one.

## Local layout

```
<root>/<app>/android/<locale>/title.txt, short_description.txt, full_description.txt, images/...
<root>/<app>/ios/metadata/<locale>/name.txt, subtitle.txt, description.txt, keywords.txt, ...
<root>/<app>/ios/screenshots/...
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

## Not supported yet

In-app purchases and subscriptions, build upload, release and rollout management,
and customer reviews. fastlane has no support for IAP, so these will use the
App Store Connect API and Google Play Developer API directly.
