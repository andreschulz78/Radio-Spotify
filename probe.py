#!/usr/bin/env python3
"""Test: Deezer (eigenes Album/Single) -> song.link -> Spotify, verglichen mit der bisherigen Wahl."""
import html, json, re, time, urllib.parse, urllib.request
import radio_spotify as r

UA = {"User-Agent": "Mozilla/5.0 (radio-spotify probe)"}


def get(url, as_json=True):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if as_json else raw
        except Exception as err:   # noqa: BLE001 – Test: Fehler protokollieren
            last = err
            time.sleep(5 * (attempt + 1))
    return {"_error": repr(last)} if as_json else ""


def deezer_pick(artist, title):
    """Bestes Deezer-Ergebnis, das vom Album/der Single des Künstlers stammt."""
    a_main, queries = r.main_artist(artist), []
    queries = [f"{a_main} {r.base_title(title)}", f"{a_main} {r.bare_title(title)}"]
    cands = []
    for q in dict.fromkeys(queries):
        res = get("https://api.deezer.com/search?" + urllib.parse.urlencode({"q": q, "limit": 15}))
        for x in res.get("data", []):
            if r.best_match([([x["artist"]["name"]], x["title"], x)], artist, title):
                cands.append(x)
        if cands:
            break
    seen, own = set(), []
    for x in cands:
        if x["id"] in seen:
            continue
        seen.add(x["id"])
        al = get(f"https://api.deezer.com/album/{x['album']['id']}")
        x["_rt"], x["_date"] = al.get("record_type"), al.get("release_date") or "9999"
        x["_al_artist"] = (al.get("artist") or {}).get("id")
        if x["_rt"] in ("album", "single", "ep") and x["_al_artist"] == x["artist"]["id"]:
            own.append(x)
    own.sort(key=lambda x: (x["_date"], x["_rt"] != "album"))
    return own[0] if own else None, len(cands)


def spotify_page(track_id):
    page = get(f"https://open.spotify.com/track/{track_id}", as_json=False)
    m = re.search(r'<meta property="og:description" content="([^"]*)"', page)
    t = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    return {"title": html.unescape(t[1]) if t else None, "desc": html.unescape(m[1]) if m else None}


out = []
for artist, title, cur in json.load(open("songs.json")):
    row = {"artist": artist, "title": title, "current": cur}
    pick, n = deezer_pick(artist, title)
    row["deezer_candidates"] = n
    if pick:
        row["deezer"] = {"id": pick["id"], "album": pick["album"]["title"], "type": pick["_rt"],
                         "date": pick["_date"], "isrc": pick.get("isrc")}
        t0 = time.time()
        sl = get("https://api.song.link/v1-alpha.1/links?" + urllib.parse.urlencode(
            {"url": f"https://www.deezer.com/track/{pick['id']}", "userCountry": "DE"}))
        row["songlink_s"] = round(time.time() - t0, 1)
        sp = (sl.get("linksByPlatform") or {}).get("spotify") or {}
        sid = (sp.get("entityUniqueId") or "").split("::")[-1] or None
        row["songlink"] = {"spotify": sid, "error": sl.get("_error") or sl.get("statusCode")}
        if sid:
            row["songlink"]["page"] = spotify_page(sid)
        time.sleep(7)   # song.link: ~10 Anfragen/Minute ohne Schlüssel
    if cur:
        row["current_page"] = spotify_page(cur.split(":")[-1])
    out.append(row)
    print(json.dumps(row, ensure_ascii=False)[:300], flush=True)
json.dump(out, open("probe_result.json", "w"), ensure_ascii=False, indent=1)
