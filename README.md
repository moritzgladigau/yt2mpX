# yt2mpX

Lokale Web-App fuer den Raspberry Pi: YouTube-Link einfuegen, `mp3` oder `mp4` waehlen, Metadaten anpassen und die fertige Datei direkt im Browser herunterladen.

> Nutze die App nur fuer Inhalte, die du rechtlich herunterladen darfst.

## Start

```bash
docker compose up --build
```

Danach im Heimnetz oeffnen:

```text
http://<pi-ip>:8000
```

Lokal auf dem Entwicklungsrechner:

```text
http://localhost:8000
```

## Funktionen

- Einzelvideos und Playlists pruefen.
- Ausgabe als `mp3` oder `mp4`.
- Titel, Artist, Album und Cover-URL vor dem Download bearbeiten.
- Qualitaetsauswahl direkt neben MP3/MP4 fuer MP3-Qualitaet oder MP4-Aufloesung; MP3 `Minimal` waehlt die kleinste Audio-Quelle, kodiert sehr klein und laesst Cover weg.
- Die zuletzt gewaehlte MP3- und MP4-Qualitaet bleibt fuer die aktuelle Browser-Session in einem Session-Cookie erhalten.
- Playlist-Tracks per flexiblem Bereich wie `1-20`, `1-20,45,60` oder `4,8,12` und per Checkboxen auswaehlen; die Bereichseingabe synchronisiert sich automatisch mit der manuellen Auswahl.
- Playlists werden als ZIP bereitgestellt.
- Nach Fertigstellung zeigt die App die Downloadgroesse an; bei ZIPs zusaetzlich die ungefaehre entpackte Groesse.
- Temp History oben rechts listet fertige Downloads der aktuellen Browser-Session, solange sie noch nicht geloescht wurden.
- Bei Playlists wird pro Track das jeweilige Video-Thumbnail als Cover eingebettet, soweit yt-dlp/ffmpeg es fuer das Ausgabeformat unterstuetzen.
- MP4-Downloads bevorzugen H.264/AAC, damit sie in QuickTime, iOS und macOS moeglichst kompatibel abspielbar sind.
- Einzelvideos werden nach den eingegebenen Metadaten als `Titel - Artist` benannt; Playlist-Dateien werden ohne YouTube-ID im Dateinamen gespeichert und doppelte Playlist-Titel bekommen automatisch `(1)`, `(2)` usw.
- Fertige Jobs werden nach `YTMPX_JOB_TTL_MINUTES` automatisch geloescht.
- Mehrere Browser/Geraete koennen parallel Jobs starten.

## Konfiguration

Die wichtigsten Werte stehen in `docker-compose.yml`:

- `YTMPX_JOB_TTL_MINUTES`: Wie lange fertige Downloads auf dem Pi bleiben.
- `YTMPX_MAX_PLAYLIST_ITEMS`: Maximale Anzahl von Playlist-Eintraegen pro Job.
- `YTMPX_PROBE_TIMEOUT_SECONDS`: Timeout fuer die Link-Pruefung.
- `YTMPX_YTDLP_SOCKET_TIMEOUT_SECONDS`: Netzwerk-Timeout fuer yt-dlp.
- `./downloads:/data`: Lokaler Speicher fuer temporaere Job-Dateien. Fertige Jobs liegen unter `./downloads/jobs/<job_id>/`.

## Qualitaet

- MP3 `Hoch`: hohe VBR-Qualitaet.
- MP3 `Mittel`: ca. 192 kbps und Standardauswahl.
- MP3 `Klein`: ca. 128 kbps.
- MP3 `Minimal`: kleinste Audio-Quelle, ca. 32 kbps und ohne Cover.
- MP4 `Beste kompatible`: beste verfuegbare H.264/AAC-Variante.
- MP4 `1080p`, `720p`, `360p`: begrenzt die maximale Videohoehe und bevorzugt weiter H.264/AAC.

## Hinweise

- Fuer private oder altersbeschraenkte Inhalte kann spaeter Cookie-Unterstuetzung ergaenzt werden.
- Version 1 hat absichtlich keinen Login und sollte nur im vertrauenswuerdigen Heimnetz erreichbar sein.
