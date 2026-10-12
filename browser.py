"""Camoufox (stealth Firefox) with a persistent, logged-in profile, for Google web UIs whose API is
closed to us (AdMob writes). Log in once with `store-mcp-browser-login`, then tools run headless."""
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from camoufox.async_api import AsyncCamoufox
from camoufox.fingerprints import get_random_preset
from camoufox.pkgman import launch_path
from camoufox.utils import resolve_verstr

PROFILE = Path(os.environ.get("STORE_BROWSER_PROFILE", "~/.config/store-mcp/browser")).expanduser()
ALLOWED = re.compile(r"^https://(admob\.google\.com|apps\.admob\.com|accounts\.google\.com)(/|$)")
# Clicks that commit a change need confirm=True, so the user sees the filled form first.
COMMIT = re.compile(r"\b(save|create|submit|publish|delete|remove|archive|confirm|apply|done|"
                    r"activate|enable|disable|pause|link|finish)\b", re.I)


def options(headless: bool, profile: Path = PROFILE) -> dict:
    """Same fingerprint every launch: a changing identity makes Google ask to sign in again."""
    PROFILE.mkdir(parents=True, exist_ok=True)
    fp = PROFILE.parent / "browser-fingerprint.json"
    if not fp.exists():
        fp.write_text(json.dumps(get_random_preset(os="macos", ff_version=resolve_verstr(launch_path()).split(".")[0])))
    return {"persistent_context": True, "user_data_dir": str(profile), "headless": headless,
            "os": "macos", "fingerprint_preset": json.loads(fp.read_text()), "locale": "en-US", "firefox_user_prefs": {"browser.startup.page": 0,
            "browser.sessionstore.resume_from_crash": False},
            "window": (1440, 900), "humanize": True}


class Browser:
    def __init__(self):
        self.cm = self.ctx = self.copy = None

    async def launch(self, profile: Path):
        self.cm = AsyncCamoufox(**options(headless=True, profile=profile))
        try:
            self.ctx = await self.cm.__aenter__()
        except Exception:
            self.cm = None
            raise

    async def page(self):
        if not self.ctx:
            try:
                await self.launch(PROFILE)
            except Exception:
                # Profile in use by another store-mcp (another Claude session) or the login window:
                # run this one from a throwaway copy, which carries the same signed-in session.
                self.copy = Path(tempfile.mkdtemp(prefix="store-mcp-browser-"))
                shutil.copytree(PROFILE, self.copy, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("lock", ".parentlock"))
                await self.launch(self.copy)
        return self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()

    async def close(self):
        if self.cm:
            await self.cm.__aexit__(None, None, None)
        if self.copy:
            shutil.rmtree(self.copy, ignore_errors=True)
        self.cm = self.ctx = self.copy = None

    async def snapshot(self, limit: int = 20000) -> str:
        p = await self.page()
        await p.wait_for_load_state("domcontentloaded")
        if "accounts.google.com" in p.url:
            return f"{p.url}\nNot signed in (or session expired): ask the user to run store-mcp-browser-login."
        tree = await p.locator("body").aria_snapshot()
        return f"{p.url} | {await p.title()}\n" + (tree if len(tree) <= limit else tree[:limit] + "\n... (truncated)")

    async def open(self, url: str) -> str:
        if not ALLOWED.match(url):
            raise ValueError("only admob.google.com / apps.admob.com URLs are allowed")
        p = await self.page()
        try:
            await p.goto(url, wait_until="domcontentloaded")
        except Exception as e:  # AdMob's own redirects can abort the first navigation
            if "ABORT" not in str(e):
                raise
        await self.settle(p)
        return await self.snapshot()

    async def settle(self, p):
        """AdMob keeps fetching after load; wait for the network to calm down (max 15 s)."""
        try:
            await p.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await p.wait_for_timeout(1000)

    async def click(self, selector: str, confirm: bool) -> str:
        p = await self.page()
        loc = p.locator(selector).first
        label = (await loc.inner_text(timeout=5000) or selector)[:80]
        if COMMIT.search(label) and not confirm:
            return (f"'{label}' commits a change. Take admob_ui_screenshot, show the user what will be saved, "
                    "and only after they approve call again with confirm=true.")
        await loc.click(timeout=10000)
        await self.settle(p)
        return await self.snapshot()

    async def fill(self, selector: str, value: str) -> str:
        p = await self.page()
        loc = p.locator(selector).first
        tag = await loc.evaluate("e => e.tagName")
        if tag == "SELECT":
            await loc.select_option(label=value)
        else:
            await loc.fill(value, timeout=10000)
        await p.wait_for_timeout(500)
        return await self.snapshot()

    async def screenshot(self) -> bytes:
        return await (await self.page()).screenshot(type="png")


def login():
    """store-mcp-browser-login [url]: open the profile with a window so you can sign in to Google."""
    import asyncio
    url = sys.argv[1] if len(sys.argv) > 1 else "https://admob.google.com/v2/home"

    async def run():
        async with AsyncCamoufox(**options(headless=False)) as ctx:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url)
            print("Sign in, then close the browser window to save the session.", flush=True)
            while ctx.pages:  # macOS keeps the app running after the last window closes
                await asyncio.sleep(1)
    asyncio.run(run())
    print(f"saved session in {PROFILE}")


if __name__ == "__main__":
    for label in ("Save", "Create ad unit", "DONE", 'role=button[name="Delete"]', "Enable"):
        assert COMMIT.search(label), label
    for label in ("Add ad unit", "Next", "Apps", "Rewarded", "Cancel"):
        assert not COMMIT.search(label), label
    assert ALLOWED.match("https://apps.admob.com/v2/home") and not ALLOWED.match("https://evil.com/?apps.admob.com")
    print("ok")
