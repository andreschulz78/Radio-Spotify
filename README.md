# Radio → Spotify

Spiegelt die Radio-Playlist  in eine Spotify-Playlist: jede Ausstrahlung der letzten 7 Tage, neueste oben.
Aktualisiert jede Minute per GitHub-Actions-Dauerlauf (Jobs à ~5 h 40 min, nahtlos aneinandergereiht); `state.json` hält den Stand.

- `radio_spotify.py` – Sync (`--dry-run` liest nur Radio aus)
- `auth.py` – einmalige Spotify-Anmeldung, legt die Playlist an
- Secrets: `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`, `SPOTIFY_PLAYLIST_ID`

Hinweis: Spotify-Apps im Development Mode brauchen ein aktives Premium-Abo des App-Inhabers.
Manuelle Änderungen an der Playlist lösen beim nächsten Lauf einen Neuaufbau aus.
