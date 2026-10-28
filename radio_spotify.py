#!/usr/bin/env python3
"""Radio-Playlist -> Spotify (rollierend 7 Tage, neueste oben, jede Ausstrahlung einzeln).

Umgebungsvariablen: RADIO_URL (Basis-URL des Senders), RADIO_TITLE_PREFIX (optional, Regex für
                    Rubrik vor dem Titel), RADIO_PLAYLIST_NAME/_DESC (optional), SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN, SPOTIFY_PLAYLIST_ID
Aufruf:  python3 radio_spotify.py            # synchronisieren
         python3 radio_spotify.py --dry-run  # nur Radio-Playlist auslesen, nichts an Spotify senden
         python3 radio_spotify.py --loop 340 # Dauerlauf: 340 Min. lang jede Minute
         … --reset                           # vorher Playlist leeren, Stand löschen
"""
import base64
import hashlib
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
BASE = os.environ.get("RADIO_URL", "").rstrip("/")   # bewusst nicht im Repo
WINDOW = timedelta(days=7)
MAX_LOOKUPS = 3            # neue Titel pro Durchlauf (je 1–2 Spotify-Anfragen; zu viele -> 24 h Sperre)
RETRY_NOT_FOUND = timedelta(days=3)
SYNC_EVERY = timedelta(minutes=15)   # Playlist nur alle 15 Min. abgleichen (Spotify-Tageskontingent)
PRUNE_HOUR = 4                       # einmal täglich ab 4 Uhr: Neuaufbau, entfernt Titel älter als 7 Tage
MATCHER = 4               # erhöhen, wenn find_uri geändert wird -> Cache verwerfen (Titel in der Playlist bleiben)
FRESH_START = datetime(2026, 10, 15, 12, 35, tzinfo=ZoneInfo("Europe/Berlin"))   # einmalig: Playlist leeren, nur noch Neues
STATE_FILE = Path(__file__).with_name("state.json")
STATUS_FILE = Path(__file__).with_name("status.json")   # letzter Fehler, im Repo sichtbar (Logs sind es nicht)
UA = "Mozilla/5.0 (radio-spotify; private Playlist)"
TITLE_PREFIX = os.environ.get("RADIO_TITLE_PREFIX", "")
PLAYLIST_DETAILS = {k: v for k, v in (("name", os.environ.get("RADIO_PLAYLIST_NAME")),     # optional, nur als Secret
                                       ("description", os.environ.get("RADIO_PLAYLIST_DESC"))) if v}

# IPv6 zum Sender hängt teils 40 s -> nur IPv4
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
        artist, title = clean(ar[1]), strip_prefix(clean(ti[1]))
        if artist and title:
            out.append({"id": f"{t:%Y-%m-%dT%H:%M}|{artist}|{title}", "t": t.isoformat(),
                        "artist": artist, "title": title})
    return out


def scrape(now, latest, start=None):
    """Neue Ausstrahlungen seit `latest` (bzw. der letzten 7 Tage, frühestens ab `start`)."""
    floor = max(now - WINDOW, start) if start else now - WINDOW
    since = max(floor, latest or floor) - timedelta(hours=6)
    day, found = since.date(), []
    while day <= now.date():
        for path, start in sorted(day_shows(day).items(), key=lambda x: x[1]):
            if since <= start <= now:
                try:
                    found += show_tracks(path, start)
                except (urllib.error.URLError, TimeoutError) as err:   # eine Sendung darf nicht alles blockieren
                    print(f"  Sendung {start:%d.%m. %H:%M} nicht abrufbar: {err}")
        day += timedelta(days=1)
    return found


# ---------- Spotify ----------

class RateLimited(RuntimeError):
    def __init__(self, msg, wait):
        super().__init__(msg)
        self.wait = wait


class Spotify:
    def __init__(self):
        self.token, self.calls = None, 0   # calls: Anfragen an die Web-API (fürs Tageskontingent)
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
            self.calls += 1
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                if method != "GET":
                    time.sleep(0.5)   # Schreibzugriffe nicht im Schwall (Spotify sperrt sonst)
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = int(e.headers.get("Retry-After", "5")) + 1
                    if wait > 60:
                        raise RateLimited(f"{method} {path}: Spotify verlangt {wait} s Pause", wait) from e
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


def strip_prefix(t):
    """Rubrik des Senders vor dem Titel entfernen (bleibt so auch aus state.json heraus)."""
    t = t.strip()
    return re.sub(rf"^(?:{TITLE_PREFIX})\s*[-/:]\s*", "", t, flags=re.I) if TITLE_PREFIX else t


def base_title(t):
    t = strip_prefix(t)
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


def best_match(cands, artist, title):
    """cands: [(Künstlernamen, Titel, Wert)] -> Wert des besten Treffers (oder None)."""
    a_main = main_artist(artist)
    swapped = re.fullmatch(r"([^,&]+),\s*([^,&]+)", a_main)   # "Green, Adam" -> "Adam Green"
    alts = {strip_the(norm(a_main))} | ({strip_the(norm(f"{swapped[2]} {swapped[1]}"))} if swapped else set())
    na_full = norm(artist)

    def artist_ok(n):
        n = strip_the(norm(n))
        return n and (n in alts or n in na_full or any(a in n or difflib.SequenceMatcher(None, n, a).ratio() > 0.85
                                                       for a in alts))

    best, best_score = None, 0.0
    for names, name, value in cands:
        if value and any(artist_ok(n) for n in names):
            score = title_score(name, title)
            if score > best_score:
                best, best_score = value, score
    return best if best_score >= 0.7 else None


def deezer_isrc(artist, title):
    """Abgleich über Deezer (frei, großzügige Limits) -> ISRC der Aufnahme."""
    a_main, t_base = main_artist(artist), base_title(title)
    swapped = re.fullmatch(r"([^,&]+),\s*([^,&]+)", a_main)
    queries = [f"{a_main} {t_base}", f"{a_main} {bare_title(title)}"]
    if swapped:
        queries.append(f"{swapped[2]} {swapped[1]} {t_base}")
    for q in dict.fromkeys(queries):
        res = json.loads(fetch("https://api.deezer.com/search?" + urllib.parse.urlencode({"q": q, "limit": 10})))
        isrc = best_match([([x["artist"]["name"]], x["title"], x.get("isrc")) for x in res.get("data", [])],
                          artist, title)
        if isrc:
            return isrc
    return None


def own_release(tr):
    """Titel stammt von einem Album/einer Single des Künstlers selbst – keine Compilation, kein Sampler."""
    al = tr.get("album") or {}
    if (al.get("album_type") or "album").lower() not in ("album", "single"):
        return False
    ids = {a.get("id") for a in tr.get("artists", [])}
    return any(a.get("id") in ids for a in al.get("artists", []))


def find_uri(sp, artist, title):
    """Deezer -> ISRC -> eine exakte Spotify-Abfrage; sonst eine Spotify-Textsuche.
    Nur Veröffentlichungen des Künstlers (Album/Single); Sampler teilen die ISRC und werden übergangen."""
    try:
        isrc = deezer_isrc(artist, title)
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        print(f"  Deezer nicht erreichbar: {err}")
        isrc = None
    if isrc:
        res = sp.call("GET", "/search", {"q": f"isrc:{isrc}", "type": "track", "limit": 10, "market": "DE"})
        own = [tr for tr in res.get("tracks", {}).get("items", []) if own_release(tr)]
        if own:   # älteste Veröffentlichung = Original; bei Gleichstand Album vor Single
            own.sort(key=lambda tr: (tr["album"].get("release_date") or "9999",
                                     tr["album"].get("album_type", "album").lower() != "album"))
            return own[0]["uri"]
    time.sleep(1)
    q = f"{main_artist(artist)} {base_title(title)}"
    res = sp.call("GET", "/search", {"q": q, "type": "track", "limit": 10, "market": "DE"})
    return best_match([([x["name"] for x in tr.get("artists", [])], tr["name"], tr["uri"])
                       for tr in res.get("tracks", {}).get("items", []) if own_release(tr)], artist, title)


def sync_playlist(sp, pid, old, desired, old_snapshot):
    """Bringt die Playlist von `old` auf `desired` (Listen aus [id, uri], neueste zuerst)."""
    cur = sp.call("GET", f"/playlists/{pid}", {"fields": "snapshot_id"})["snapshot_id"]
    if old is None or cur != old_snapshot:
        return rebuild(sp, pid, desired, "Playlist weicht vom gespeicherten Stand ab")

    keep = {tuple(x) for x in desired}
    gone_uris = {u for i, u in old if (i, u) not in keep}
    uris = sorted(gone_uris)
    for k in range(0, len(uris), 100):   # entfernt jeweils alle Vorkommen der URI
        cur = sp.call("DELETE", f"/playlists/{pid}/items",
                      body={"items": [{"uri": u} for u in uris[k:k + 100]]}).get("snapshot_id", cur)
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

    snap = cur
    for start, us in runs:
        for k in range(0, len(us), 100):
            snap = sp.call("POST", f"/playlists/{pid}/items",
                           body={"uris": us[k:k + 100], "position": start + k}).get("snapshot_id", snap)
    print(f"Spotify: -{len(old) - len(remaining)} +{len(desired) - len(remaining)} -> {len(desired)} Titel")
    return snap


def rebuild(sp, pid, desired, why):
    print(f"Spotify: Neuaufbau ({why}), {len(desired)} Titel")
    uris = [u for _, u in desired]
    snap = sp.call("PUT", f"/playlists/{pid}/items", body={"uris": uris[:100]}).get("snapshot_id")
    for k in range(100, len(uris), 100):
        snap = sp.call("POST", f"/playlists/{pid}/items", body={"uris": uris[k:k + 100]}).get("snapshot_id", snap)
    return snap or sp.call("GET", f"/playlists/{pid}", {"fields": "snapshot_id"})["snapshot_id"]


# ---------- Ablauf ----------

def run_once(sp=None, dry=False):
    now = datetime.now(TZ)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if TITLE_PREFIX:   # ältere Stände: Rubrik aus gespeicherten Titeln entfernen
        fix = lambda i: "|".join(i.split("|", 2)[:2] + [strip_prefix(i.split("|", 2)[2])])
        for e in state.get("entries", []):
            e["id"], e["title"] = fix(e["id"]), strip_prefix(e["title"])
        if state.get("playlist"):
            state["playlist"] = [[fix(i), u] for i, u in state["playlist"]]
        state["cache"] = {k: v for k, v in state.get("cache", {}).items()
                          if not re.search(rf"\|(?:{TITLE_PREFIX})\b", k, flags=re.I)}
    entries = {e["id"]: e for e in state.get("entries", [])}
    cache = state.get("cache", {})
    if state.get("matcher") != MATCHER:
        cache = {}
    if sp and FRESH_START and now >= FRESH_START and state.get("fresh") != FRESH_START.isoformat():
        sp.call("PUT", f"/playlists/{os.environ['SPOTIFY_PLAYLIST_ID']}/items", body={"uris": []})
        state = {"fresh": FRESH_START.isoformat(), "start": now.isoformat(), "playlist": None}
        entries = {}
        print(f"{now:%H:%M} Neustart: Playlist geleert, ab jetzt nur neue Ausstrahlungen")
    start = datetime.fromisoformat(state["start"]) if state.get("start") else None
    pinned = {i: u for i, u in state.get("playlist") or []}   # schon in der Playlist -> nicht mehr anfassen

    latest = max((datetime.fromisoformat(e["t"]) for e in entries.values()), default=None)
    new = [e for e in scrape(now, latest, start)
           if e["id"] not in entries and (not start or datetime.fromisoformat(e["t"]) >= start)]
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

    blocked = state.get("blocked_until")
    lookups, limited = 0, bool(blocked and now < datetime.fromisoformat(blocked))
    lookup_calls = -sp.calls
    for e in sorted(entries.values(), key=lambda e: e["t"], reverse=True):
        if e["id"] in pinned:
            e["uri"] = pinned[e["id"]]
            continue
        key = norm(e["artist"]) + "|" + norm(e["title"])
        c = cache.get(key)
        stale = c and c["u"] is None and now - datetime.fromisoformat(c["d"]) > RETRY_NOT_FOUND
        if (c is None or stale) and lookups < MAX_LOOKUPS and not limited:
            try:
                cache[key] = c = {"u": find_uri(sp, e["artist"], e["title"]), "d": now.isoformat()}
            except RateLimited as err:
                print(f"  Suche abgebrochen: {err}")
                limited, blocked = True, (now + timedelta(seconds=err.wait)).isoformat()
                continue
            lookups += 1
            if c["u"] is None:
                print(f"  nicht gefunden: {e['artist']} – {e['title']}")
        e["uri"] = c["u"] if c else None
    used = {norm(e["artist"]) + "|" + norm(e["title"]) for e in entries.values()}
    cache = {k: v for k, v in cache.items() if k in used}
    lookup_calls += sp.calls
    if lookups:
        print(f"  Spotify-Suchen: {lookups}")

    ordered = sorted(entries.values(), key=lambda e: e["id"], reverse=True)
    desired = [[e["id"], e["uri"]] for e in ordered if e.get("uri")]
    pid, snapshot = os.environ["SPOTIFY_PLAYLIST_ID"], state.get("snapshot")
    details, playlist = state.get("details"), state.get("playlist")   # details: nur ein Hash (anonym)
    want = hashlib.sha256(json.dumps(PLAYLIST_DETAILS, sort_keys=True).encode()).hexdigest()[:12]
    # Tagsüber nur neue Titel einfügen (abgelaufene bleiben unten stehen), Entfernen beim täglichen Neuaufbau:
    # spart Spotify-Anfragen, denn Entfernen löscht alle Vorkommen eines Titels und erzwingt viele Einzel-Inserts.
    synced, pruned = state.get("synced"), state.get("pruned")
    old_id = f"{cutoff:%Y-%m-%dT%H:%M}"
    target = desired + [x for x in playlist or [] if x[0] < old_id and x not in desired]
    prune = playlist is None or snapshot is None or (pruned != f"{now:%Y-%m-%d}" and now.hour >= PRUNE_HOUR)
    due = prune or (target != playlist and (not synced or now - datetime.fromisoformat(synced) >= SYNC_EVERY))
    calls0 = sp.calls
    if limited:   # Spotify-Sperre: nichts senden, Radio-Stand trotzdem sichern
        if desired != playlist:
            print(f"  Spotify gesperrt bis {blocked[:16]} – Playlist wird danach abgeglichen")
    elif due or (PLAYLIST_DETAILS and details != want):
        try:
            if PLAYLIST_DETAILS and details != want:   # setzen, ohne Neuaufbau auszulösen
                before = sp.call("GET", f"/playlists/{pid}", {"fields": "snapshot_id"})["snapshot_id"]
                sp.call("PUT", f"/playlists/{pid}", body=PLAYLIST_DETAILS)
                details = want
                if before == snapshot:
                    snapshot = sp.call("GET", f"/playlists/{pid}", {"fields": "snapshot_id"})["snapshot_id"]
                print("Spotify: Name/Beschreibung der Playlist gesetzt")
            if prune:
                snapshot, playlist, pruned = rebuild(sp, pid, desired, "täglich"), desired, f"{now:%Y-%m-%d}"
                synced = now.isoformat()
            elif due:
                snapshot, playlist, synced = sync_playlist(sp, pid, playlist, target, snapshot), target, now.isoformat()
        except RateLimited as err:   # nicht jede Minute neu versuchen – das verlängert die Sperre
            print(f"  Spotify gesperrt: {err}")
            blocked = (now + timedelta(seconds=err.wait)).isoformat()
            snapshot = None   # Stand in Spotify unklar -> nach der Sperre einmal neu aufbauen

    calls = {d: n for d, n in (state.get("calls") or {}).items() if d >= f"{now - timedelta(days=7):%Y-%m-%d}"}
    today = f"{now:%Y-%m-%d}"
    calls[today] = calls.get(today, 0) + sp.calls - calls0 + lookup_calls   # Spotify-Anfragen pro Tag
    STATE_FILE.write_text(json.dumps({
        "updated": now.isoformat(), "matcher": MATCHER, "blocked_until": blocked, "details": details,
        "synced": synced, "pruned": pruned, "calls": calls,
        "fresh": state.get("fresh"), "start": state.get("start"), "snapshot": snapshot, "playlist": playlist,
        "entries": [{k: e[k] for k in ("id", "t", "artist", "title")} for e in ordered],
        "cache": cache}, ensure_ascii=False, indent=0))


def commit_state():
    """Stand ins Repo schreiben (nur in GitHub Actions)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    git = lambda *a: subprocess.run(["git", *a], cwd=STATE_FILE.parent, capture_output=True, text=True, timeout=120)
    git("fetch", "-q", "origin", "main")
    git("reset", "-q", "origin/main")   # auf neuesten Repo-Stand setzen (Dateien bleiben) – kein Rebase-Konflikt
    git("add", STATE_FILE.name, STATUS_FILE.name)   # vollständige Stände, die gewinnen
    if git("diff", "--cached", "--quiet").returncode:
        git("commit", "-q", "-m", f"Stand {datetime.now(TZ):%Y-%m-%d %H:%M}")
        r = git("push", "-q", "origin", "HEAD:main")
        if r.returncode:
            print("  git push fehlgeschlagen:", r.stderr.strip())


def write_status(status):
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=1) + "\n")


def loop(minutes, every=60, commit_every=600):
    """Dauerlauf: jede Minute synchronisieren, Stand alle 10 Min. sichern."""
    sp = Spotify()
    status = {"job_start": datetime.now(TZ).isoformat(timespec="seconds"), "last_ok": None,
              "errors_in_row": 0, "last_error": None, "last_error_at": None}
    end, last_commit = time.monotonic() + minutes * 60, time.monotonic() - commit_every   # erster Stand sofort
    while time.monotonic() < end:
        t0, pause = time.monotonic(), every
        try:
            run_once(sp)
            status.update(last_ok=datetime.now(TZ).isoformat(timespec="seconds"), errors_in_row=0)
        except Exception as err:   # nächster Versuch im nächsten Takt
            msg = f"{type(err).__name__}: {err}"
            msg = (msg.replace(BASE, "RADIO_URL") if BASE else msg)[:500]
            print(f"{datetime.now(TZ):%H:%M} Fehler: {msg}")
            status.update(last_error=msg, last_error_at=datetime.now(TZ).isoformat(timespec="seconds"),
                          errors_in_row=status["errors_in_row"] + 1)
            pause = min(err.wait, 3600) if isinstance(err, RateLimited) else every   # Spotify-Sperre abwarten
        if time.monotonic() - last_commit > commit_every:
            write_status(status)
            try:
                commit_state()
            except subprocess.TimeoutExpired:
                print("  git hängt – nächster Versuch in 10 Min.")
            last_commit = time.monotonic()
        time.sleep(max(5, min(pause - (time.monotonic() - t0), end - time.monotonic())))
    write_status(status)
    try:
        commit_state()
    except subprocess.TimeoutExpired:
        print("  git hängt – Stand nicht gesichert")


def reset():
    """Playlist leeren und gespeicherten Stand verwerfen."""
    Spotify().call("PUT", f"/playlists/{os.environ['SPOTIFY_PLAYLIST_ID']}/items", body={"uris": []})
    STATE_FILE.unlink(missing_ok=True)
    print("Reset: Playlist geleert, Stand gelöscht")


if __name__ == "__main__":
    if not BASE:
        sys.exit("RADIO_URL fehlt (Basis-URL des Senders, als GitHub-Secret bzw. Umgebungsvariable)")
    if "--reset" in sys.argv:
        reset()
    if "--dry-run" in sys.argv:
        run_once(dry=True)
    elif "--loop" in sys.argv:
        loop(int(sys.argv[sys.argv.index("--loop") + 1]))
    else:
        run_once(Spotify())
