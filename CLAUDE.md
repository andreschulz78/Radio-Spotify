# Radio → Spotify

Spiegelt die Radio-Playlist  minütlich in eine Spotify-Playlist (7 Tage rollierend, neueste oben, jede Ausstrahlung einzeln).

- Repo: https://github.com/andreschulz78/Radio-spotify (öffentlich), `gh` als andreschulz78
- Playlist: https://open.spotify.com/playlist/4co4S71IZsWv5QRNfET54J
- Läuft als GitHub-Actions-Dauerlauf (`--loop 340`, Jobs nahtlos per concurrency); Reset: Workflow manuell mit `reset=true`
- `state.json` wird vom Bot committet – vor eigenen Pushes `git pull --rebase`
- Titelabgleich: Deezer → ISRC → `isrc:`-Suche bei Spotify; max. 3 neue Titel pro Minute.
  Spotify-Dev-Mode sperrt bei Suchschüben 24 h (`blocked_until` in `state.json`) – nie in großen Mengen suchen.
- Lokal hängt IPv6 → Skripte erzwingen IPv4.
- Antworten kurz, Deutsch. Rückfragen als Klickoptionen, Empfehlung zuerst.
