#!/usr/bin/env python3
"""
POTD Tracker: local web app.

    python3 app.py          # starts the tracker and opens http://localhost:8765

While it runs, a background thread checks the potd.lol leaderboards every few
minutes (Chrome stays minimized), opens a handicapper profile only when they
posted something new, and the page lists the bets to place: units,
target odds and the lowest odds still worth taking. Ticked bets turn green.
"""
import json
import queue
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import potd
import updater
from potd import LoginRequired, NoData, to_dt

HOST, PORT = "127.0.0.1", 8765
HERE = Path(__file__).resolve().parent
RES = Path(getattr(sys, "_MEIPASS", HERE))  # bundled files when packaged
STATE_FILE = potd.DATA_DIR / "data" / "state.json"
INDEX_HTML = RES / "web" / "index.html"

DEFAULT_SETTINGS = {
    "multiplier": 1.0,      # scale the handicapper's units
    "tolerancePct": 3.0,    # lowest acceptable odds = target odds minus this %
    "pollMinutes": 5,
}
NEW_BADGE_HOURS = 6
SETTLE_RECHECK_MIN = 30
PAUSE_RANGE = (1.5, 3.0)


def now_utc():
    return datetime.now(timezone.utc)


def iso(d):
    return d.isoformat() if d else None


def notify(title, text):
    try:
        if potd.IS_MAC:
            subprocess.run(["osascript", "-e",
                            f"display notification {json.dumps(text)} with title {json.dumps(title)}"],
                           check=False)
        elif potd.IS_WIN:
            q = lambda v: "'" + v.replace("'", "''") + "'"
            ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                  "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;"
                  f"$n.BalloonTipTitle={q(title)};$n.BalloonTipText={q(text)};$n.Visible=$true;"
                  "$n.ShowBalloonTip(8000);Start-Sleep 9;$n.Dispose()")
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                             creationflags=0x08000000)
    except Exception:
        pass


def bet_key(p):
    """Same bet from several handicappers shares one key (match + pick text)."""
    pick = re.sub(r"\s+", " ", p["pick"].lower()).strip()
    return (p.get("match") or "") + "|" + pick


# ====================================================================== tracker

class Tracker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.RLock()
        self.cmds = queue.Queue()
        self.chrome = potd.HiddenChrome()
        self.state = self._load()
        self.status = {"phase": "starting", "message": "Starting…", "lastCheck": None,
                       "nextCheck": None, "progress": None}
        self.next_due = time.time()
        self.stopped = threading.Event()
        self.auto_prompted = False  # open the sign-in page by itself once per run

    # ---------- persistence

    def _load(self):
        s = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        s.setdefault("picks", {})
        s.setdefault("users", {})        # uid -> {lastPickAt, lastVisited}
        s.setdefault("leaderboard", {})  # uid -> leaderboard row + lists
        s.setdefault("placed", {})       # bet key -> {at}
        saved = s.get("settings", {})
        s["settings"] = {k: saved.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
        s.pop("last_run", None)
        return s

    def save(self):
        with self.lock:
            STATE_FILE.parent.mkdir(exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state))
            tmp.replace(STATE_FILE)

    def set_status(self, **kw):
        with self.lock:
            self.status.update(kw)

    # ---------- main loop (the only thread that touches Chrome)

    def run(self):
        while not self.stopped.is_set():
            try:
                cmd = self.cmds.get(timeout=1)
            except queue.Empty:
                cmd = None
            try:
                if cmd == "quit":
                    break
                if cmd == "login":
                    self.set_status(phase="login", message="Sign in to potd.lol in the Chrome window, then click “I’m signed in”.")
                    self.chrome.show(potd.BASE + "/sign-in")
                    continue
                elif cmd == "show":
                    self.chrome.show()
                    continue
                elif cmd == "hide":
                    self.chrome.hide()
                    continue
                # while waiting for the user to sign in, only check when asked
                waiting = self.status["phase"] in ("login", "login_required")
                if cmd in ("check", "login_done") or (not waiting and time.time() >= self.next_due):
                    self.check(after_login=(cmd == "login_done"))
            except Exception as ex:  # keep the loop alive whatever happens
                print("tracker error:", repr(ex), file=sys.stderr)
                self.chrome.close()
                self.set_status(phase="error", message=f"Something went wrong: {ex}. Retrying soon.")
                self._schedule()
        self.chrome.close()
        self.stopped.set()

    def _schedule(self):
        minutes = max(2, float(self.state["settings"]["pollMinutes"]))
        self.next_due = time.time() + minutes * 60
        self.set_status(nextCheck=iso(now_utc() + timedelta(minutes=minutes)))

    def check(self, after_login=False):
        self.set_status(phase="checking", message="Checking leaderboards…", progress=None)
        page = self.chrome.ensure()
        try:
            users = potd.fetch_leaderboards(page)
        except LoginRequired:
            self.set_status(phase="login_required", progress=None,
                            message="Sign in to your potd.lol account to start tracking.")
            if not self.auto_prompted and not after_login:
                # first run (or expired login): bring up the sign-in page right away
                self.auto_prompted = True
                self.cmds.put("login")
                return
            if after_login:
                self.set_status(phase="login", message="Still not signed in. Finish signing in in Chrome, then click “I’m signed in”.")
            return
        except NoData as ex:
            self.set_status(phase="attention", progress=None,
                            message="potd.lol didn’t load (it may be showing a human check). Click Show browser to look.")
            print("no data:", ex, file=sys.stderr)
            self._schedule()
            return
        if after_login:
            self.chrome.hide()

        with self.lock:
            self.state["leaderboard"] = users
            known_users = self.state["users"]
            picks = self.state["picks"]
            now = now_utc()
            stale = {p["handicapperId"] for p in picks.values()
                     if p["status"] in ("pending", "unreviewed") and to_dt(p["kickoff"])
                     and now - timedelta(days=3) < to_dt(p["kickoff"]) < now - timedelta(hours=2)}
            to_visit = []
            for uid, u in users.items():
                k = known_users.get(uid)
                if k is None or k.get("lastPickAt") != u.get("lastPickAt"):
                    to_visit.append(u)
                elif uid in stale and (not k.get("lastVisited") or
                                       now - to_dt(k["lastVisited"]) > timedelta(minutes=SETTLE_RECHECK_MIN)):
                    to_visit.append(u)

        new_picks = []
        for i, u in enumerate(to_visit, 1):
            if self.stopped.is_set() or not self.cmds.empty():
                break  # pick up user commands; the rest is checked next round
            self.set_status(message=f"Reading {u.get('displayName') or u['username']}…", progress=[i, len(to_visit)])
            time.sleep(random.uniform(*PAUSE_RANGE))
            try:
                fetched = potd.fetch_picks(page, u)
            except NoData as ex:
                print("skip:", ex, file=sys.stderr)
                continue
            with self.lock:
                first_scan = u["uniqueId"] not in self.state["users"]
                for p in fetched:
                    prev = self.state["picks"].get(p["id"])
                    if prev:
                        p["firstSeen"] = prev["firstSeen"]
                        p["live"] = prev.get("live", False)
                    else:
                        p["firstSeen"] = iso(now_utc())
                        p["live"] = not first_scan
                        if p["live"] and p["status"] in ("pending", "unreviewed"):
                            new_picks.append(p)
                    self.state["picks"][p["id"]] = p
                self.state["users"][u["uniqueId"]] = {"lastPickAt": u.get("lastPickAt"),
                                                      "lastVisited": iso(now_utc())}
            self.save()

        self.save()
        if new_picks:
            names = ", ".join(sorted({p["handicapper"] for p in new_picks}))
            notify("New POTD picks", f"{len(new_picks)} new: {names}")
        self.set_status(phase="idle", progress=None, lastCheck=iso(now_utc()),
                        message="Watching for new picks.")
        self._schedule()

    # ---------- view model for the page

    def view(self):
        with self.lock:
            s = self.state
            st = s["settings"]
            lb = s["leaderboard"]
            placed = s["placed"]
            now = now_utc()
            window = now - timedelta(days=3)

            groups = {}
            for p in s["picks"].values():
                key = bet_key(p)
                k = to_dt(p["kickoff"]) or to_dt(p["added"])
                if key not in placed and (p["handicapperId"] not in lb or not k or k < window):
                    continue
                groups.setdefault(key, []).append(p)

            bets = []
            for key, ps in groups.items():
                open_ = any(p["status"] in ("pending", "unreviewed") for p in ps)
                is_placed = key in placed
                kickoff = min((to_dt(p["kickoff"]) for p in ps if p["kickoff"]), default=None)
                if kickoff and kickoff < now - timedelta(hours=12):
                    continue  # long over
                if not is_placed and (not open_ or (kickoff and kickoff < now)):
                    continue  # started or settled without being placed: no longer relevant
                ps.sort(key=lambda p: -float(p.get("units") or 0))
                units = max(float(p.get("units") or 1) for p in ps) * float(st["multiplier"])
                odds = [float(p["decOdds"]) for p in ps if p.get("decOdds")]
                target = min(odds) if odds else None
                target_pick = next((p for p in ps if p.get("decOdds") and float(p["decOdds"]) == target), ps[0])
                min_odds = max(1.01, target * (1 - float(st["tolerancePct"]) / 100)) if target else None
                result = None
                if not open_:
                    result = next((p["status"] for p in ps if p["status"] in ("won", "lost", "push")), None)
                bets.append({
                    "key": key,
                    "pick": ps[0]["pick"],
                    "league": ps[0]["league"],
                    "kickoff": iso(kickoff),
                    "units": round(units, 2),
                    "targetOdds": target,
                    "targetUs": target_pick.get("usOdds"),
                    "minOdds": round(min_odds, 2) if min_odds else None,
                    "placed": is_placed,
                    "result": result,
                    "isNew": any(p.get("live") and to_dt(p["firstSeen"]) > now - timedelta(hours=NEW_BADGE_HOURS)
                                 for p in ps) and not is_placed,
                    "unreviewed": all(p["status"] == "unreviewed" for p in ps),
                    "handicappers": [{
                        "name": p["handicapper"], "profile": p["profile"],
                        "units": p.get("units"), "odds": p.get("decOdds"),
                        "rating": lb.get(p["handicapperId"], {}).get("rating"),
                        "roi": lb.get(p["handicapperId"], {}).get("roi"),
                        "lists": lb.get(p["handicapperId"], {}).get("lists", []),
                    } for p in ps],
                })
            far = "9999"
            bets.sort(key=lambda b: (b["kickoff"] or far, b["pick"]))

            tracked = sorted(({
                "name": u.get("displayName") or u["username"],
                "profile": f"{potd.BASE}/handicapper/{potd.ident(u)}",
                "rating": u.get("rating"), "roi": u.get("roi"), "units": u.get("unitsWon"),
                "entries": u.get("entries"), "lastTen": u.get("lastTen"),
                "lastPickAt": u.get("lastPickAt"), "lists": u.get("lists", []),
            } for u in lb.values()), key=lambda u: -float(u["rating"] or 0))
            return {"status": dict(self.status), "settings": st, "bets": bets,
                    "tracked": tracked, "now": iso(now)}

    # ---------- actions from the page

    def set_placed(self, key, is_placed):
        with self.lock:
            if is_placed:
                self.state["placed"][key] = {"at": iso(now_utc())}
            else:
                self.state["placed"].pop(key, None)
        self.save()

    def update_settings(self, new):
        with self.lock:
            st = self.state["settings"]
            for k in DEFAULT_SETTINGS:
                if k in new:
                    st[k] = float(new[k])
            st["pollMinutes"] = max(2.0, st["pollMinutes"])
        self.save()
        self._schedule_from_settings()

    def _schedule_from_settings(self):
        if self.status["phase"] == "idle":
            minutes = float(self.state["settings"]["pollMinutes"])
            last = to_dt(self.status["lastCheck"]) if self.status["lastCheck"] else now_utc()
            due = last + timedelta(minutes=minutes)
            self.next_due = due.timestamp()
            self.set_status(nextCheck=iso(due))


# ====================================================================== web

TRACKER = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/update":
            self._send(200, updater.check())
        elif self.path == "/api/state":
            self._send(200, TRACKER.view())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        # only accept requests from our own page
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/placed":
            TRACKER.set_placed(str(body["key"]), bool(body.get("placed")))
        elif self.path == "/api/settings":
            TRACKER.update_settings(body)
        elif self.path == "/api/update_apply":
            return self._send(200, updater.apply(shutdown))
        elif self.path in ("/api/check", "/api/login", "/api/login_done", "/api/show", "/api/hide"):
            TRACKER.cmds.put(self.path.rsplit("/", 1)[1])
        elif self.path == "/api/quit":
            self._send(200, {"ok": True})
            threading.Thread(target=shutdown, daemon=True).start()
            return
        else:
            return self._send(404, {"error": "not found"})
        self._send(200, TRACKER.view())


SERVER = None


def shutdown():
    TRACKER.cmds.put("quit")
    TRACKER.stopped.wait(timeout=20)
    SERVER.shutdown()


def main():
    global TRACKER, SERVER
    url = f"http://{HOST}:{PORT}"
    try:  # already running? just open the page
        urllib.request.urlopen(url + "/api/state", timeout=2)
        webbrowser.open(url)
        print("POTD Tracker is already running:", url)
        return
    except Exception:
        pass
    updater.cleanup()
    TRACKER = Tracker()
    TRACKER.start()
    SERVER = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"POTD Tracker running at {url}  (Ctrl-C or the Quit button to stop)")
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
        TRACKER.cmds.put("quit")
        TRACKER.stopped.wait(timeout=20)


if __name__ == "__main__":
    main()
