"""
Core POTD.LOL access: readers for the leaderboard and handicapper data, plus
the sign-in helper.

Data is read over plain HTTPS with the user's own session cookie. Chrome is only
opened (visibly) for signing in, because the sign-in page has a human check.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
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
CHROME_PROFILE = DATA_DIR / ".potd-chrome"
CHROME_APP = "Google Chrome"
DEBUG_PORT = 9333

LEADERBOARDS = [
    # (label, API query)
    ("Rating", "limit=25&metric=rating&minPicks=8"),
    ("ROI 30d", "limit=25&metric=roi&days=30&minPicks=8"),
]


class LoginRequired(Exception):
    """The site answered 401: the saved sign-in is gone."""


class NoData(Exception):
    """The page did not load its data (Cloudflare check, site down, ...)."""


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


class SignIn(threading.Thread):
    """Opens a normal, visible Chrome on the potd.lol sign-in page (the site's
    human check only works in a real browser), waits until the user is signed
    in, hands the session cookie to on_done(cookie) and closes Chrome.
    on_done(None) if the window was closed first or cancel() was called."""

    def __init__(self, on_done):
        super().__init__(daemon=True)
        self.on_done = on_done
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        cookie = None
        pw = None
        try:
            shutil.rmtree(CHROME_PROFILE, ignore_errors=True)  # fresh: any cookie we see is a new sign-in
            flags = [f"--user-data-dir={CHROME_PROFILE}", f"--remote-debugging-port={DEBUG_PORT}",
                     "--no-first-run", "--no-default-browser-check",
                     "--window-size=1000,800", BASE + "/sign-in"]
            if IS_MAC:
                subprocess.run(["open", "-n", "-a", CHROME_APP, "--args", *flags], check=True)
            else:
                subprocess.Popen([_chrome_exe(), *flags])
            for _ in range(60):
                if _port_open():
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Chrome did not start")
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}", timeout=20000)
            while not self._cancel.is_set() and browser.is_connected() and _port_open():
                try:
                    for c in browser.contexts[0].cookies(BASE):
                        if c["name"] == SESSION_COOKIE and is_signed_in(c["value"]):
                            cookie = c["value"]
                except Exception:
                    pass
                if cookie:
                    break
                time.sleep(1)
            if browser.is_connected():
                try:
                    browser.new_browser_cdp_session().send("Browser.close")
                except Exception:
                    pass
        except Exception as ex:
            print("sign-in error:", repr(ex), file=sys.stderr)
        finally:
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass
            shutil.rmtree(CHROME_PROFILE, ignore_errors=True)
            self.on_done(cookie)


# ---------------------------------------------------------------- HTTP

SESSION_COOKIE = "_potd"
# A browser-like UA with a non-browser TLS fingerprint trips Cloudflare's challenge; a plain one passes.
UA = "Mozilla/5.0"


def api_get(path, cookie):
    """GET https://www.potd.lol/api<path> with the user's session. -> (status, json|None)"""
    req = urllib.request.Request(BASE + "/api" + path, headers={
        "User-Agent": UA, "Accept": "application/json",
        **({"Cookie": f"{SESSION_COOKIE}={cookie}"} if cookie else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, b""
    except Exception as e:
        raise NoData(f"{path}: {e}")
    try:
        return status, json.loads(raw)
    except Exception:
        return status, None


def is_signed_in(cookie):
    try:
        status, body = api_get("/check-sign-in", cookie)
    except NoData:
        return False
    return status == 200 and isinstance(body, dict) and bool(body.get("loggedIn"))


# ---------------------------------------------------------------- readers

def fetch_leaderboards(cookie):
    """Union of both top-25 lists, keyed by handicapper uniqueId."""
    users = {}
    for label, query in LEADERBOARDS:
        status, rows = api_get("/api/leaderboards?" + query, cookie)
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


def fetch_picks(cookie, user):
    """Latest 50 picks plus any awaiting admin review."""
    i = ident(user)
    status, body = api_get(f"/handicapper/{i}?startRow=0&endRow=50", cookie)
    if status == 401:
        raise LoginRequired()
    if status != 200:
        raise NoData(f"Profile {i}: HTTP {status}")
    picks = []
    if isinstance(body, dict):
        picks += [_normalize_pick(r, user) for r in body.get("rows") or []]
    aw = api_get(f"/handicapper/{i}/awaiting-review", cookie)[1]
    if isinstance(aw, list):
        picks += [_normalize_pick(r, user, unreviewed=True) for r in aw]
    return picks
