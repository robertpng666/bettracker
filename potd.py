"""
Core POTD.LOL access: a hidden Chrome session plus readers for the leaderboard
and handicapper pages.

The site only serves its data to its own web app in a real, signed-in browser.
So we drive the user's installed Google Chrome with a dedicated profile
(.potd-chrome), open the same pages a person would, and read the data those
pages load. The Chrome window is kept minimized; it is only shown when the
user has to sign in.

All functions here must be called from one thread (Playwright sync API).
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

BASE = "https://www.potd.lol"
HERE = Path(__file__).resolve().parent
FROZEN = getattr(sys, "frozen", False)
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
# Each user's own data (state, Chrome profile with their own potd.lol login).
# A packaged exe keeps it in the user profile so updating the exe never touches it.
if FROZEN and IS_WIN:
    DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "POTDTracker"
else:
    DATA_DIR = HERE
LOG_FILE = DATA_DIR / "data" / "tracker.log"
CHROME_PROFILE = DATA_DIR / ".potd-chrome"
CHROME_APP = "Google Chrome"
DEBUG_PORT = 9333

LEADERBOARDS = [
    # (label, page url, metric, days)
    ("Rating", BASE + "/leaderboards", "rating", None),
    ("ROI 30d", BASE + "/leaderboards?limit=25&metric=roi&range=30", "roi", "30"),
]


class LoginRequired(Exception):
    """The site answered 401: the saved sign-in is gone."""


class NoData(Exception):
    """The page did not load its data (Cloudflare check, site down, ...)."""


def log(*a):
    """Append to data/tracker.log (the packaged app has no console)."""
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 500_000:
            LOG_FILE.replace(LOG_FILE.with_suffix(".old"))
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
    except Exception:
        pass


def to_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def ident(u):
    return f"{u['username']}-{u['uniqueId']}"


# ---------------------------------------------------------------- Chrome

def _port_open():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=1)
        return True
    except Exception:
        return False


def _chrome_exe():
    """Path to the installed Chrome on Windows/Linux."""
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            p = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if p.exists():
                return str(p)
    for name in ("chrome", "google-chrome", "chrome.exe"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError("Google Chrome is not installed. Install it from google.com/chrome and start the tracker again.")


class HiddenChrome:
    """One Chrome instance with the POTD profile, kept minimized."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self.page = None
        self.launched_by_us = False

    def ensure(self):
        if self.page and not self.page.is_closed() and self._browser.is_connected():
            return self.page
        self._disconnect()
        if not _port_open():
            # `open -g` starts Chrome without bringing it to the front;
            # the window starts off-screen and is minimized right after.
            flags = [
                f"--user-data-dir={CHROME_PROFILE}",
                f"--remote-debugging-port={DEBUG_PORT}",
                "--no-first-run", "--no-default-browser-check",
                "--window-position=-3000,-3000", "--window-size=1100,850",
                # keep pages working normally while minimized
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "about:blank",
            ]
            if IS_MAC:
                subprocess.run(["open", "-g", "-n", "-a", CHROME_APP, "--args", *flags], check=True)
            else:
                subprocess.Popen([_chrome_exe(), *flags])
            for _ in range(60):
                if _port_open():
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Chrome did not start")
            self.launched_by_us = True
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{DEBUG_PORT}", timeout=20000)
        ctx = self._browser.contexts[0]
        self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self.hide()
        return self.page

    def signed_in(self):
        """True when the page's own session is signed in (asked from inside the browser)."""
        try:
            return bool(self.page.evaluate(
                "fetch('/api/check-sign-in',{credentials:'include'}).then(r=>r.json()).then(j=>!!j.loggedIn)"))
        except Exception:
            return False

    def _window(self):
        cdp = self.page.context.new_cdp_session(self.page)
        return cdp, cdp.send("Browser.getWindowForTarget")["windowId"]

    def hide(self):
        cdp, wid = self._window()
        cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "minimized"}})

    def show(self, url=None):
        self.ensure()
        cdp, wid = self._window()
        cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {
            "left": 120, "top": 80, "width": 1100, "height": 850}})
        if url:
            self.page.goto(url, wait_until="commit")
        self.page.bring_to_front()
        if IS_MAC:
            subprocess.run(["osascript", "-e", f'tell application "{CHROME_APP}" to activate'], check=False)

    def close(self):
        if self._browser and self._browser.is_connected():
            try:
                self._browser.new_browser_cdp_session().send("Browser.close")
            except Exception:
                pass
        self._disconnect()

    def _disconnect(self):
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self.page = None


def load_page_data(page, url, matchers, timeout=30):
    """Navigate to url and return {name: (status, json)} for API responses the
    page itself loads. matchers: {name: fn(response_url) -> bool}. The first
    matcher is required; the rest are best effort."""
    seen = {}

    def on_response(r):
        for name, fn in matchers.items():
            if name not in seen and fn(r.url):
                seen[name] = r

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="commit", timeout=timeout * 1000)
        required = next(iter(matchers))
        deadline = time.time() + timeout
        while required not in seen and time.time() < deadline:
            page.wait_for_timeout(250)
        if required not in seen:
            try:
                where = f"page “{page.title()[:60]}” at {page.url[:80]}"
            except Exception:
                where = "page not readable"
            raise NoData(f"No data loaded from {url} ({where})")
        grace = time.time() + 1.5
        while len(seen) < len(matchers) and time.time() < grace:
            page.wait_for_timeout(250)
        out = {}
        for name, r in seen.items():
            try:
                body = r.json()
            except Exception:
                body = None
            out[name] = (r.status, body)
        return out
    finally:
        page.remove_listener("response", on_response)


# ---------------------------------------------------------------- readers

def _lb_matcher(metric, days):
    def fn(u):
        if "/api/api/leaderboards" not in u:
            return False
        q = parse_qs(urlparse(u).query)
        return q.get("metric", [None])[0] == metric and q.get("days", [None])[0] == days
    return fn


def fetch_leaderboards(page):
    """Union of both top-25 lists, keyed by handicapper uniqueId."""
    users = {}
    for label, url, metric, days in LEADERBOARDS:
        status, rows = load_page_data(page, url, {"lb": _lb_matcher(metric, days)})["lb"]
        if status == 401:
            raise LoginRequired()
        if status != 200 or not isinstance(rows, list):
            raise NoData(f"Leaderboard {label}: HTTP {status}")
        for rank, r in enumerate(rows[:25], 1):
            u = users.setdefault(r["uniqueId"], dict(r, lists=[]))
            u["lists"].append(f"{label} #{rank}")
    return users


def _normalize_pick(row, user, unreviewed=False):
    if unreviewed:
        status = "unreviewed"
    elif row.get("win") == 1:
        status = "won"
    elif row.get("lose") == 1:
        status = "lost"
    elif row.get("push") == 1:
        status = "push"
    else:
        status = "pending"
    return {
        "id": row.get("uniqueId") or row.get("entryUniqueId") or str(row.get("id")),
        "handicapper": user.get("displayName") or user["username"],
        "handicapperId": user["uniqueId"],
        "profile": f"{BASE}/handicapper/{ident(user)}",
        "league": row.get("leagueName") or "",
        "pick": row.get("pickProp") or row.get("aiSuggestedPickProp") or "",
        "units": row.get("units") or row.get("aiSuggestedUnits"),
        "decOdds": row.get("decOdds"),
        "usOdds": row.get("usOdds") or row.get("aiSuggestedUsOdds"),
        "kickoff": row.get("matchKickoff") or row.get("matchDate"),
        "added": row.get("dateAdded"),
        "match": row.get("matchUniqueId"),
        "status": status,
        "unitsResult": row.get("unitsResult"),
    }


def fetch_picks(page, user):
    """Latest 50 picks plus any awaiting admin review."""
    i = ident(user)
    data = load_page_data(page, f"{BASE}/handicapper/{i}", {
        "picks": lambda u: f"/api/handicapper/{i}?" in u,
        "awaiting": lambda u: u.endswith(f"/api/handicapper/{i}/awaiting-review"),
    })
    status, body = data["picks"]
    if status == 401:
        raise LoginRequired()
    picks = []
    if isinstance(body, dict):
        picks += [_normalize_pick(r, user) for r in body.get("rows") or []]
    aw = data.get("awaiting", (None, None))[1]
    if isinstance(aw, list):
        picks += [_normalize_pick(r, user, unreviewed=True) for r in aw]
    return picks
