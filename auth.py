#!/usr/bin/env python3
"""Einmalige Spotify-Anmeldung: holt den Refresh-Token und legt die Playlist an.

Voraussetzung: Spotify-App mit Redirect-URI http://127.0.0.1:8888/callback
Aufruf: python3 auth.py
"""
import base64
import getpass
import http.server
import json
import secrets
import urllib.parse
import urllib.request
import webbrowser

REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-modify-public playlist-modify-private playlist-read-private"
NAME = "Radio – Live (7 Tage)"

cid = input("Client ID: ").strip()
sec = getpass.getpass("Client Secret (wird nicht angezeigt): ").strip()
state = secrets.token_urlsafe(16)
code = None


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global code
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        ok = q.get("state", [""])[0] == state and "code" in q
        code = q["code"][0] if ok else None
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Fertig – Fenster schließen.".encode() if ok else "Fehler bei der Anmeldung.".encode())

    def log_message(self, *a):
        pass


url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
    {"client_id": cid, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES, "state": state})
print("Browser öffnet sich für die Spotify-Anmeldung …")
webbrowser.open(url)
http.server.HTTPServer(("127.0.0.1", 8888), Handler).handle_request()
if not code:
    raise SystemExit("Anmeldung fehlgeschlagen.")

req = urllib.request.Request("https://accounts.spotify.com/api/token", data=urllib.parse.urlencode(
    {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT}).encode(), headers={
    "Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(),
    "Content-Type": "application/x-www-form-urlencoded"})
tok = json.load(urllib.request.urlopen(req))

req = urllib.request.Request("https://api.spotify.com/v1/me/playlists", method="POST", data=json.dumps({
    "name": NAME, "public": True,
    "description": "Was Radio  in den letzten 7 Tagen gespielt hat – neueste oben, automatisch aktualisiert."
}).encode(), headers={"Authorization": f"Bearer {tok['access_token']}", "Content-Type": "application/json"})
pl = json.load(urllib.request.urlopen(req))

print("\nPlaylist angelegt:", pl["external_urls"]["spotify"])
print("\nDiese vier Werte als GitHub-Secrets eintragen (Repo → Settings → Secrets and variables → Actions):")
print("  SPOTIFY_CLIENT_ID      =", cid)
print("  SPOTIFY_CLIENT_SECRET  = (dein Client Secret)")
print("  SPOTIFY_REFRESH_TOKEN  =", tok["refresh_token"])
print("  SPOTIFY_PLAYLIST_ID    =", pl["id"])
