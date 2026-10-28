# Radio → Spotify

Spiegelt die Playlist eines Radiosenders alle 4 Minuten in eine Spotify-Playlist (7 Tage rollierend, neueste oben, jede Ausstrahlung einzeln).

- Repo: https://github.com/andreschulz78/radio-spotify (öffentlich), `gh` als andreschulz78
- Anonym halten: Sender nirgends im Repo, in Commits oder Logs nennen. URL steht nur im Secret `RADIO_URL`, Rubrik-Präfix im Secret `RADIO_TITLE_PREFIX`, Playlist-Name/-Beschreibung (dürfen den Sender nennen) in `RADIO_PLAYLIST_NAME`/`_DESC` – in `state.json` nur als Hash.
- Playlist: https://open.spotify.com/playlist/4co4S71IZsWv5QRNfET54J
- Einmaliger Neustart 15.10.2026 12:35 (`FRESH_START`): Playlist geleert, danach nur neue Ausstrahlungen
- Läuft als GitHub-Actions-Dauerlauf (`--loop 340`, Jobs nahtlos per concurrency); Reset: Workflow manuell mit `reset=true`
- `state.json` wird vom Bot committet – vor eigenen Pushes `git pull --rebase`. Bot: Checkout mit `ref: main` (eingereihte Läufe hätten sonst einen stundenalten Stand) und Speichern per fetch + reset auf `origin/main`, nur `state.json` wird committet.
- Titelabgleich: Deezer → ISRC → `isrc:`-Suche bei Spotify; max. 3 neue Titel pro Durchlauf.
  Nur Album/Single des Künstlers selbst (keine Sampler/Compilations, die teilen die ISRC). Titel, die schon in der Playlist sind, werden nicht nachträglich getauscht.
  Spotify hat ein Tageskontingent (Sperre bis ~05:15 Uhr, `blocked_until` in `state.json`; während der Sperre keine Anfrage). Deshalb: Takt 4 Min. (`loop(every=240)`, `SYNC_EVERY`), keine Kontrollabfrage vor dem Abgleich, tagsüber nur Einfügen, abgelaufene Titel entfernt ein täglicher Neuaufbau ab 4 Uhr (`PRUNE_HOUR`). Harte Obergrenze `DAILY_BUDGET` = 600 Anfragen pro Kontingent-Tag (ab 05:30 Uhr), danach bis zum Morgen nichts an Spotify. Anfragen pro Tag stehen in `state.json` unter `calls`.
- Lokal hängt IPv6 → Skripte erzwingen IPv4.
- Antworten kurz, Deutsch. Rückfragen als Klickoptionen, Empfehlung zuerst.
