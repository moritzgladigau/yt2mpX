# yt2mpX

Lokale Web-App für den Raspberry Pi: YouTube-Link einfügen, MP3 oder MP4 wählen, Metadaten prüfen und die fertige Datei im Browser herunterladen.

> Nutze die App nur für Inhalte, die du rechtlich herunterladen darfst.

## Start

```bash
docker compose up --build
```

Danach im Heimnetz öffnen:

```text
http://<pi-ip>:8000
```

Lokal auf dem Entwicklungsrechner:

```text
http://localhost:8000
```

## Funktionen

- Einzelvideos und Playlists prüfen.
- Ausgabe als `mp3` oder `mp4`.
- Ablauf: Link prüfen, Format, Qualität und Playlist-Einträge wählen; die App lädt und konvertiert die Medien, schlägt Metadaten vor und stellt die Datei nach deren Prüfung und dem Tagging zum Herunterladen bereit.
- Mit konfiguriertem AcoustID-Key sucht die App nach dem Download per Chromaprint/fpcalc, AcoustID und MusicBrainz nach Musik-Metadaten. Ohne Key oder sicheren Treffer verwendet sie YouTube-Daten als Fallback.
- Titel, Artist, Album, Datum/Jahr, Track-/Discnummer, ISRC, Cover-URL und MusicBrainz-IDs können vor dem finalen Tagging bearbeitet werden.
- Qualitätsauswahl für MP3-Bitrate oder MP4-Auflösung; MP3 `Minimal` wählt die kleinste Audio-Quelle.
- Bei MP3 steuert eine Checkbox, ob Cover eingebettet werden. Qualität und Checkbox werden in einem Browser-Session-Cookie gespeichert; die Kennung für die Temp History liegt im lokalen Browserspeicher.
- Playlist-Tracks per flexiblem Bereich wie `1-20`, `1-20,45,60` oder `4,8,12` und per Checkboxen auswählen; die Bereichseingabe synchronisiert sich automatisch mit der manuellen Auswahl.
- Playlists werden als ZIP bereitgestellt.
- Nach Fertigstellung zeigt die App die Downloadgröße an; bei ZIPs zusätzlich die ungefähre entpackte Größe.
- Temp History oben rechts listet fertige Downloads dieses Browsers, solange sie auf dem Server verfügbar sind.
- Bei Playlists wird pro Track eine eigene Metadaten-Erkennung versucht; nicht erkannte Tracks behalten YouTube-/Fallback-Tags.
- Cover bevorzugen MusicBrainz/Cover Art Archive; wenn dieses Cover nicht geladen werden kann, nutzt die App das YouTube-Thumbnail als Fallback. MP3 schreibt nur dann ein Cover, wenn die Cover-Checkbox aktiv ist.
- MP4-Downloads bevorzugen H.264/AAC für bessere Kompatibilität mit QuickTime, iOS und macOS; wenn diese Formate nicht verfügbar sind, werden andere Varianten versucht.
- Einzelvideos werden nach den eingegebenen Metadaten als `Titel - Artist` benannt; Playlist-Dateien werden ohne YouTube-ID im Dateinamen gespeichert und doppelte Playlist-Titel bekommen automatisch `(1)`, `(2)` usw.
- Fertige Jobs werden nach der letzten Aktualisierung gemäß `YTMPX_JOB_TTL_MINUTES` automatisch gelöscht (Prüfung alle fünf Minuten).
- Mehrere Browser/Geräte können parallel Jobs starten.

## Konfiguration

Die wichtigsten Werte stehen in `docker-compose.yml`. Weitere Umgebungsvariablen können bei Bedarf ergänzt werden:

- `YTMPX_JOB_TTL_MINUTES`: Wie lange fertige Downloads auf dem Pi bleiben.
- `YTMPX_MAX_PLAYLIST_ITEMS`: Maximale Anzahl von Playlist-Einträgen pro Job.
- `YTMPX_PROBE_TIMEOUT_SECONDS`: Timeout für die Link-Prüfung (Standard: 75 Sekunden; nicht in `docker-compose.yml` gesetzt).
- `YTMPX_YTDLP_SOCKET_TIMEOUT_SECONDS`: Netzwerk-Timeout für yt-dlp (Standard: 30 Sekunden; nicht in `docker-compose.yml` gesetzt).
- `YTMPX_ACOUSTID_API_KEY`: AcoustID Application API Key für die Audio-Fingerprint-Erkennung. Ohne Key läuft die App weiter und nutzt Fallback-Metadaten.
- `YTMPX_MUSICBRAINZ_USER_AGENT`: User-Agent für MusicBrainz, idealerweise mit Kontaktinfo.
- `./downloads:/data`: Lokaler Speicher für temporäre Job-Dateien. Fertige Jobs liegen unter `./downloads/jobs/<job_id>/`.

Beispiel für den AcoustID-Key:

```bash
YTMPX_ACOUSTID_API_KEY=dein_key docker compose up --build
```

## Qualität

- MP3 `Hoch`: hohe VBR-Qualität, `VBR 0`.
- MP3 `Mittel`: ca. 192 kbps und Standardauswahl.
- MP3 `Klein`: ca. 128 kbps.
- MP3 `Minimal`: kleinste Audio-Quelle, Zielqualität 32 kbps.
- MP4 `Beste kompatible`: bevorzugt die beste verfügbare H.264/AAC-Variante.
- MP4 `1080p`, `720p`, `360p`: versucht, die Videohöhe zu begrenzen, und bevorzugt H.264/AAC; als letzter Fallback kann eine andere Auflösung gewählt werden.

## Hinweise

- Für private oder altersbeschränkte Inhalte kann später Cookie-Unterstützung ergänzt werden.
- Version 1 hat absichtlich keinen Login und sollte nur im vertrauenswürdigen Heimnetz erreichbar sein.
