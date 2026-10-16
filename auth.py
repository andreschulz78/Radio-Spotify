#!/usr/bin/env python3
"""Einmalige Spotify-Anmeldung: holt den Refresh-Token und legt die Playlist an.

Voraussetzung: Spotify-App mit Redirect-URI http://127.0.0.1:8888/callback
Aufruf: python3 auth.py
"""
import base64
import getpass
import json
import socket
import subprocess
import urllib.parse
import urllib.request
import webbrowser

REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-modify-public playlist-modify-private playlist-read-private"
REPO = "andreschulz78/radio-spotify"
NAME = "Radio – Live (7 Tage)"

# IPv6 hängt auf diesem Mac -> nur IPv4
_gai = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: [x for x in _gai(*a, **k) if x[0] == socket.AF_INET] or _gai(*a, **k)

cid = input("Client ID: ").strip()
sec = getpass.getpass("Client Secret (wird nicht angezeigt): ").strip()
url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
    {"client_id": cid, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES})
print("\n1. Diesen Link im Browser öffnen und zustimmen:\n\n" + url)
print("\n2. Der Browser landet auf einer Fehlerseite (127.0.0.1 …) – das ist richtig.")
webbrowser.open(url)
back = input("3. Komplette Adresse aus der Adresszeile hier einfügen: ").strip()
q = urllib.parse.parse_qs(urllib.parse.urlparse(back).query)
if "code" not in q:
    raise SystemExit("Kein code in der Adresse gefunden.")
code = q["code"][0]

req = urllib.request.Request("https://accounts.spotify.com/api/token", data=urllib.parse.urlencode(
    {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT}).encode(), headers={
    "Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(),
    "Content-Type": "application/x-www-form-urlencoded"})
tok = json.load(urllib.request.urlopen(req))

req = urllib.request.Request("https://api.spotify.com/v1/me/playlists", method="POST", data=json.dumps({
    "name": NAME, "public": True,
    "description": "Was im Radio in den letzten 7 Tagen lief – neueste oben, automatisch aktualisiert."
}).encode(), headers={"Authorization": f"Bearer {tok['access_token']}", "Content-Type": "application/json"})
pl = json.load(urllib.request.urlopen(req))

print("\nPlaylist angelegt:", pl["external_urls"]["spotify"])
values = {"SPOTIFY_CLIENT_ID": cid, "SPOTIFY_CLIENT_SECRET": sec,
          "SPOTIFY_REFRESH_TOKEN": tok["refresh_token"], "SPOTIFY_PLAYLIST_ID": pl["id"]}
for name, val in values.items():
    r = subprocess.run(["gh", "secret", "set", name, "-R", REPO], input=val, text=True, capture_output=True)
    print(f"  GitHub-Secret {name}: {'gesetzt' if r.returncode == 0 else 'FEHLER ' + r.stderr.strip()}")
