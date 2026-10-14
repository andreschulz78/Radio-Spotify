#!/usr/bin/env python3
"""Radio-Playlist -> Spotify (rollierend 7 Tage, neueste oben, jede Ausstrahlung einzeln).

Umgebungsvariablen: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN, SPOTIFY_PLAYLIST_ID
Aufruf:  python3 radio_spotify.py            # synchronisieren
         python3 radio_spotify.py --dry-run  # nur Radio auslesen, nichts an Spotify senden
         python3 radio_spotify.py --loop 340 # Dauerlauf: 340 Min. lang jede Minute
"""
import base64
import difflib
import html
import json
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
BASE = "https://example.invalid"
WINDOW = timedelta(days=7)
MAX_LOOKUPS = 300          # Spotify-Suchen pro Lauf (Rest folgt im nächsten Lauf)
RETRY_NOT_FOUND = timedelta(days=3)
MATCHER = 2               # erhöhen, wenn find_uri verbessert wird -> Nicht-Gefundene neu suchen
STATE_FILE = Path(__file__).with_name("state.json")
UA = "Mozilla/5.0 (Radio-spotify; private Playlist)"

# IPv6 zu Radio hängt teils 40 s -> nur IPv4
_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: [x for x in _getaddrinfo(*a, **k) if x[0] == socket.AF_INET] or _getaddrinfo(*a, **k)


# ---------- Radio ----------

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(5)


def day_shows(day):
    """Sendungs-Playlist-URLs eines Tages mit Startzeit."""
    nxt = day + timedelta(days=1)
    url = f"{BASE}/musik/playlists.htm/from={day:%d-%m-%Y}_00-00/to={nxt:%d-%m-%Y}_00-00.html"
    page = fetch(url)
    shows = {}
    for path, ymd, hm in re.findall(r'href="(/programm/[^"]*/playlisten/\d{4}/\d{2}/(\d{6})_(\d{4})\.html)"', page):
        start = datetime.strptime(ymd + hm, "%y%m%d%H%M").replace(tzinfo=TZ)
        shows[path] = start
    return shows


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def show_tracks(path, start):
    page = fetch(BASE + path)
    out = []
    for row in re.findall(r'<tr class="play_track">(.*?)</tr>', page, re.S):
        tm = re.search(r'class="play_time">\s*(\d{1,2}):(\d{2})', row)
        ar = re.search(r'class="trackinterpret[^"]*">(.*?)</span>', row, re.S)
        ti = re.search(r'class="tracktitle">(.*?)</span>', row, re.S)
        if not (tm and ar and ti):
            continue
        t = start.replace(hour=int(tm[1]), minute=int(tm[2]))
        if t < start - timedelta(hours=1):   # Sendung über Mitternacht
            t += timedelta(days=1)
        artist, title = clean(ar[1]), clean(ti[1])
        if artist and title:
            out.append({"id": f"{t:%Y-%m-%dT%H:%M}|{artist}|{title}", "t": t.isoformat(),
                        "artist": artist, "title": title})
    return out


def scrape(now, latest):
    """Neue Ausstrahlungen seit `latest` (bzw. der letzten 7 Tage)."""
    since = max(now - WINDOW, latest) - timedelta(hours=6) if latest else now - WINDOW - timedelta(hours=6)
    day, found = since.date(), []
    while day <= now.date():
        for path, start in sorted(day_shows(day).items(), key=lambda x: x[1]):
            if since <= start <= now:
                found += show_tracks(path, start)
        day += timedelta(days=1)
    return found


# ---------- Spotify ----------

class RateLimited(RuntimeError):
    pass


class Spotify:
    def __init__(self):
        self.token = None
        self.refresh()

    def refresh(self):
        cid, sec = os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"]
        body = urllib.parse.urlencode({"grant_type": "refresh_token",
                                       "refresh_token": os.environ["SPOTIFY_REFRESH_TOKEN"]}).encode()
        req = urllib.request.Request("https://accounts.spotify.com/api/token", data=body, headers={
            "Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            self.token = json.load(r)["access_token"]

    def call(self, method, path, params=None, body=None):
        url = "https://api.spotify.com/v1" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(6):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = int(e.headers.get("Retry-After", "5")) + 1
                    if wait > 60:
                        raise RateLimited(f"{method} {path}: Spotify verlangt {wait} s Pause") from e
                    time.sleep(wait)
                elif e.code == 401 and attempt == 0:
                    self.refresh()
                elif e.code >= 500 and attempt < 5:
                    time.sleep(3)
                else:
                    raise RuntimeError(f"{method} {path}: {e.code} {e.read()[:300]!r}") from e
        raise RuntimeError(f"{method} {path}: zu viele Versuche")


def norm(s):
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def main_artist(a):
    return re.split(r"\s+(?:feat\.?|ft\.?|featuring|vs\.?|x|with)\s+", a, maxsplit=1, flags=re.I)[0].strip()


def base_title(t):
    t = re.sub(r"^Rubrik\s*[-/:]\s*", "", t.strip(), flags=re.I)   # Radio-Rubrik vor dem Titel
    t = re.sub(r"\s*[\(\[][^)\]]*(edit|version|mix|remaster|single|radio|feat|live|mono|stereo)[^)\]]*[\)\]]",
               "", t, flags=re.I)
    return re.sub(r"\s+-\s+.*(edit|version|remaster).*$", "", t, flags=re.I).strip() or t


def strip_the(s):
    return s[4:] if s.startswith("the ") else s


def bare_title(t):
    """Titel ohne jede Klammer und ohne ' - Zusatz'."""
    return norm(re.sub(r"\s*[\(\[].*?[\)\]]|\s+-\s+.*$", "", base_title(t))) or norm(t)


def title_score(found, wanted):
    a, b = bare_title(found), bare_title(wanted)
    if a == b:
        return 1.0
    return max(difflib.SequenceMatcher(None, norm(base_title(found)), norm(base_title(wanted))).ratio(),
               difflib.SequenceMatcher(None, a, b).ratio(),
               0.85 if a.startswith(b + " ") or b.startswith(a + " ") else 0.0)


def find_uri(sp, artist, title):
    a_main, t_base = main_artist(artist), base_title(title)
    swapped = re.fullmatch(r"([^,&]+),\s*([^,&]+)", a_main)   # "Green, Adam" -> "Adam Green"
    alts = {strip_the(norm(a_main))} | ({strip_the(norm(f"{swapped[2]} {swapped[1]}"))} if swapped else set())
    na_full = norm(artist)

    def artist_ok(n):
        n = strip_the(n)
        return n and (n in alts or n in na_full or any(a in n or difflib.SequenceMatcher(None, n, a).ratio() > 0.85
                                                       for a in alts))

    queries = [f"track:{t_base} artist:{a_main}", f"{a_main} {t_base}", f"{a_main} {bare_title(title)}"]
    if swapped:
        queries.append(f"{swapped[2]} {swapped[1]} {t_base}")
    best, best_score = None, 0.0
    for q in dict.fromkeys(queries):
        time.sleep(0.3)   # Spotify-Ratenlimit schonen
        res = sp.call("GET", "/search", {"q": q, "type": "track", "limit": 10, "market": "DE"})
        for tr in res.get("tracks", {}).get("items", []):
            if not any(artist_ok(norm(x["name"])) for x in tr.get("artists", [])):
                continue
            score = title_score(tr["name"], title)
            if score > best_score:
                best, best_score = tr["uri"], score
        if best_score >= 0.9:
            break
    return best if best_score >= 0.7 else None


def sync_playlist(sp, pid, old, desired, old_snapshot):
    """Bringt die Playlist von `old` auf `desired` (Listen aus [id, uri], neueste zuerst)."""
    cur = sp.call("GET", f"/playlists/{pid}", {"fields": "snapshot_id"})["snapshot_id"]
    if old is None or cur != old_snapshot:
        return rebuild(sp, pid, desired, "Playlist weicht vom gespeicherten Stand ab")

    keep = {tuple(x) for x in desired}
    gone_uris = {u for i, u in old if (i, u) not in keep}
    uris = sorted(gone_uris)
    for k in range(0, len(uris), 100):   # entfernt jeweils alle Vorkommen der URI
        sp.call("DELETE", f"/playlists/{pid}/items", body={"items": [{"uri": u} for u in uris[k:k + 100]]})
    remaining = [x for x in old if x[1] not in gone_uris]

    runs, p = [], 0
    for idx, (i, u) in enumerate(desired):
        if p < len(remaining) and remaining[p][0] == i:
            p += 1
        elif runs and runs[-1][0] + len(runs[-1][1]) == idx:
            runs[-1][1].append(u)
        else:
            runs.append((idx, [u]))
    if p != len(remaining):
        return rebuild(sp, pid, desired, "Reihenfolge inkonsistent")

    snap = None
    for start, us in runs:
        for k in range(0, len(us), 100):
            snap = sp.call("POST", f"/playlists/{pid}/items", body={"uris": us[k:k + 100], "position": start + k})
    print(f"Spotify: -{len(old) - len(remaining)} +{len(desired) - len(remaining)} -> {len(desired)} Titel")
    return sp.call("GET", f"/playlists/{pid}", {"fields": "snapshot_id"})["snapshot_id"]


def rebuild(sp, pid, desired, why):
    print(f"Spotify: Neuaufbau ({why}), {len(desired)} Titel")
    uris = [u for _, u in desired]
    sp.call("PUT", f"/playlists/{pid}/items", body={"uris": uris[:100]})
    for k in range(100, len(uris), 100):
        sp.call("POST", f"/playlists/{pid}/items", body={"uris": uris[k:k + 100]})
    return sp.call("GET", f"/playlists/{pid}", {"fields": "snapshot_id"})["snapshot_id"]


# ---------- Ablauf ----------

def run_once(sp=None, dry=False):
    now = datetime.now(TZ)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    entries = {e["id"]: e for e in state.get("entries", [])}
    cache = state.get("cache", {})
    if state.get("matcher") != MATCHER:
        cache = {k: v for k, v in cache.items() if v["u"]}

    latest = max((datetime.fromisoformat(e["t"]) for e in entries.values()), default=None)
    new = [e for e in scrape(now, latest) if e["id"] not in entries]
    for e in new:
        entries[e["id"]] = e
    cutoff = now - WINDOW
    entries = {i: e for i, e in entries.items() if datetime.fromisoformat(e["t"]) >= cutoff}
    if new or dry:
        print(f"{now:%H:%M} Radio: {len(new)} neu, {len(entries)} im 7-Tage-Fenster")
    if dry:
        for e in sorted(new, key=lambda e: e["t"])[-15:]:
            print(f"  {e['t'][:16]}  {e['artist']} – {e['title']}")
        return

    lookups, limited = 0, False
    for e in sorted(entries.values(), key=lambda e: e["t"], reverse=True):
        key = norm(e["artist"]) + "|" + norm(e["title"])
        c = cache.get(key)
        stale = c and c["u"] is None and now - datetime.fromisoformat(c["d"]) > RETRY_NOT_FOUND
        if (c is None or stale) and lookups < MAX_LOOKUPS and not limited:
            try:
                cache[key] = c = {"u": find_uri(sp, e["artist"], e["title"]), "d": now.isoformat()}
            except RateLimited as err:
                print(f"  Suche abgebrochen: {err}")
                limited = True
                continue
            lookups += 1
            if c["u"] is None:
                print(f"  nicht gefunden: {e['artist']} – {e['title']}")
        e["uri"] = c["u"] if c else None
    used = {norm(e["artist"]) + "|" + norm(e["title"]) for e in entries.values()}
    cache = {k: v for k, v in cache.items() if k in used}
    if lookups:
        print(f"  Spotify-Suchen: {lookups}")

    ordered = sorted(entries.values(), key=lambda e: e["id"], reverse=True)
    desired = [[e["id"], e["uri"]] for e in ordered if e.get("uri")]
    snapshot = state.get("snapshot")
    if desired != state.get("playlist"):
        snapshot = sync_playlist(sp, os.environ["SPOTIFY_PLAYLIST_ID"], state.get("playlist"), desired, snapshot)

    STATE_FILE.write_text(json.dumps({
        "updated": now.isoformat(), "matcher": MATCHER, "snapshot": snapshot, "playlist": desired,
        "entries": [{k: e[k] for k in ("id", "t", "artist", "title")} for e in ordered],
        "cache": cache}, ensure_ascii=False, indent=0))


def commit_state():
    """Stand ins Repo schreiben (nur in GitHub Actions)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    git = lambda *a: subprocess.run(["git", *a], cwd=STATE_FILE.parent, capture_output=True, text=True)
    git("add", STATE_FILE.name)
    if git("diff", "--cached", "--quiet").returncode:
        git("commit", "-q", "-m", f"Stand {datetime.now(TZ):%Y-%m-%d %H:%M}")
        git("pull", "-q", "--rebase")
        r = git("push", "-q")
        if r.returncode:
            print("  git push fehlgeschlagen:", r.stderr.strip())


def loop(minutes, every=60, commit_every=600):
    """Dauerlauf: jede Minute synchronisieren, Stand alle 10 Min. sichern."""
    sp = Spotify()
    end, last_commit = time.monotonic() + minutes * 60, time.monotonic()
    while time.monotonic() < end:
        t0, pause = time.monotonic(), every
        try:
            run_once(sp)
        except Exception as err:   # nächster Versuch im nächsten Takt
            print(f"{datetime.now(TZ):%H:%M} Fehler: {err}")
            pause = 300 if isinstance(err, RateLimited) else every
        if time.monotonic() - last_commit > commit_every:
            commit_state()
            last_commit = time.monotonic()
        time.sleep(max(5, min(pause - (time.monotonic() - t0), end - time.monotonic())))
    commit_state()


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        run_once(dry=True)
    elif "--loop" in sys.argv:
        loop(int(sys.argv[sys.argv.index("--loop") + 1]))
    else:
        run_once(Spotify())
