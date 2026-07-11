const form = document.querySelector("#probe-form");
const urlInput = document.querySelector("#url");
const notice = document.querySelector("#notice");
const result = document.querySelector("#result");
const thumbnail = document.querySelector("#thumbnail");
const mediaType = document.querySelector("#media-type");
const mediaTitle = document.querySelector("#media-title");
const mediaCount = document.querySelector("#media-count");
const entries = document.querySelector("#entries");
const playlistDetails = document.querySelector("#playlist-details");
const playlistRule = document.querySelector("#playlist-rule");
const playlistSummary = document.querySelector("#playlist-summary");
const manualActions = document.querySelector("#manual-actions");
const selectAllButton = document.querySelector("#select-all");
const selectNoneButton = document.querySelector("#select-none");
const selectInvertButton = document.querySelector("#select-invert");
const longVideoWarning = document.querySelector("#long-video-warning");
const longVideoMessage = document.querySelector("#long-video-message");
const excludeLongVideosButton = document.querySelector("#exclude-long-videos");
const startButton = document.querySelector("#start-button");
const probeButton = document.querySelector("#probe-button");
const jobBox = document.querySelector("#job");
const jobMessage = document.querySelector("#job-message");
const jobPercent = document.querySelector("#job-percent");
const jobProgress = document.querySelector("#job-progress");
const downloadLink = document.querySelector("#download-link");
const downloadSize = document.querySelector("#download-size");
const historyToggle = document.querySelector("#history-toggle");
const historyCount = document.querySelector("#history-count");
const historyPanel = document.querySelector("#history-panel");
const historyRefresh = document.querySelector("#history-refresh");
const historyEmpty = document.querySelector("#history-empty");
const historyList = document.querySelector("#history-list");

const metaTitle = document.querySelector("#meta-title");
const metaArtist = document.querySelector("#meta-artist");
const metaAlbum = document.querySelector("#meta-album");
const metaCover = document.querySelector("#meta-cover");
const metaCoverLabel = metaCover.closest("label");
const qualitySelect = document.querySelector("#quality-select");

let currentProbe = null;
let currentJob = null;
let pollTimer = null;
let selectedPlaylistItems = new Set();
let selectedAudioQuality = "medium";
let selectedVideoQuality = "best_compatible";
const longVideoThresholdSeconds = 10 * 60;
const qualityPreferenceCookie = "yt2mpx-quality-preferences";

const qualityOptions = {
  mp3: [
    ["high", "Hoch"],
    ["medium", "Mittel"],
    ["small", "Klein"],
    ["minimal", "Minimal"],
  ],
  mp4: [
    ["best_compatible", "Beste kompatible"],
    ["1080", "1080p"],
    ["720", "720p"],
    ["360", "360p"],
  ],
};

const sessionIdKey = "yt2mpx-session-id";
let sessionId = localStorage.getItem(sessionIdKey);
if (!sessionId) {
  sessionId = crypto.randomUUID?.() || `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  localStorage.setItem(sessionIdKey, sessionId);
}

function readCookie(name) {
  const prefix = `${name}=`;
  const cookie = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix));
  return cookie ? cookie.slice(prefix.length) : "";
}

function isValidQuality(format, value) {
  return qualityOptions[format]?.some(([optionValue]) => optionValue === value) || false;
}

function loadQualityPreferences() {
  const rawPreferences = readCookie(qualityPreferenceCookie);
  if (!rawPreferences) return;
  try {
    const preferences = JSON.parse(decodeURIComponent(rawPreferences));
    if (isValidQuality("mp3", preferences.audio)) {
      selectedAudioQuality = preferences.audio;
    }
    if (isValidQuality("mp4", preferences.video)) {
      selectedVideoQuality = preferences.video;
    }
  } catch {
    document.cookie = `${qualityPreferenceCookie}=; path=/; max-age=0; SameSite=Lax`;
  }
}

function saveQualityPreferences() {
  const preferences = encodeURIComponent(JSON.stringify({
    audio: selectedAudioQuality,
    video: selectedVideoQuality,
  }));
  document.cookie = `${qualityPreferenceCookie}=${preferences}; path=/; SameSite=Lax`;
}

function setNotice(text, isError = false) {
  notice.textContent = text;
  notice.classList.toggle("error", isError);
}

function resetJobUi() {
  clearInterval(pollTimer);
  pollTimer = null;
  currentJob = null;
  jobBox.classList.add("hidden");
  downloadLink.classList.add("hidden");
  downloadLink.removeAttribute("href");
  downloadLink.removeAttribute("download");
  downloadSize.classList.add("hidden");
  downloadSize.textContent = "";
  jobProgress.value = 0;
  jobPercent.textContent = "0%";
  jobMessage.textContent = "Wartet";
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const digits = value >= 10 || unitIndex === 0 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[unitIndex]}`;
}

function formatRemaining(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "läuft gleich ab";
  const minutes = Math.max(1, Math.ceil(seconds / 60));
  return minutes === 1 ? "noch 1 min" : `noch ${minutes} min`;
}

function updateDownloadSize(data) {
  const downloadText = formatBytes(data.download_size_bytes);
  const unpackedText = formatBytes(data.unpacked_size_bytes);
  if (!downloadText) {
    downloadSize.classList.add("hidden");
    downloadSize.textContent = "";
    return;
  }

  downloadSize.textContent = unpackedText
    ? `Download: ${downloadText} ZIP, entpackt ca. ${unpackedText}`
    : `Download: ${downloadText}`;
  downloadSize.classList.remove("hidden");
}

function renderHistory(items) {
  historyCount.textContent = items.length;
  historyEmpty.classList.toggle("hidden", items.length > 0);
  historyList.replaceChildren();

  for (const item of items) {
    const li = document.createElement("li");
    const link = document.createElement("a");
    link.href = item.download_url;
    link.download = item.download_name || "";
    link.textContent = item.download_name || "Download";

    const meta = document.createElement("span");
    meta.textContent = [
      formatBytes(item.download_size_bytes),
      item.is_playlist ? "ZIP" : item.media_format?.toUpperCase(),
      formatRemaining(item.remaining_seconds),
    ].filter(Boolean).join(" / ");

    li.append(link, meta);
    historyList.appendChild(li);
  }
}

async function refreshHistory() {
  try {
    const response = await fetch(`/api/history?session_id=${encodeURIComponent(sessionId)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "History nicht verfügbar");
    renderHistory(data.items || []);
  } catch {
    historyCount.textContent = "!";
  }
}

function playlistPositions() {
  return (currentProbe?.entries || []).map((item) => item.position).filter((position) => Number.isInteger(position));
}

function longVideoPositions() {
  return (currentProbe?.entries || [])
    .filter((item) => Number.isFinite(item.duration) && item.duration > longVideoThresholdSeconds)
    .map((item) => item.position);
}

function parseNumberList(text, validPositions) {
  const valid = new Set(validPositions);
  const parsed = new Set();
  for (const rawPart of text.split(",")) {
    const part = rawPart.trim();
    if (!part) continue;
    const rangeMatch = part.match(/^(\d+)\s*-\s*(\d+)$/);
    if (rangeMatch) {
      const start = Number(rangeMatch[1]);
      const end = Number(rangeMatch[2]);
      const min = Math.min(start, end);
      const max = Math.max(start, end);
      for (let value = min; value <= max; value += 1) {
        if (valid.has(value)) parsed.add(value);
      }
      continue;
    }
    const value = Number(part);
    if (Number.isInteger(value) && valid.has(value)) {
      parsed.add(value);
    }
  }
  return parsed;
}

function formatNumberRanges(selectedPositions) {
  const sorted = [...selectedPositions].sort((a, b) => a - b);
  const parts = [];
  let start = null;
  let previous = null;

  for (const position of sorted) {
    if (start === null) {
      start = position;
      previous = position;
      continue;
    }
    if (position === previous + 1) {
      previous = position;
      continue;
    }
    parts.push(start === previous ? `${start}` : `${start}-${previous}`);
    start = position;
    previous = position;
  }

  if (start !== null) {
    parts.push(start === previous ? `${start}` : `${start}-${previous}`);
  }

  return parts.join(",");
}

function syncRuleFromSelection() {
  playlistRule.value = formatNumberRanges(selectedPlaylistItems);
}

function syncCheckboxes() {
  for (const checkbox of entries.querySelectorAll("input[type='checkbox']")) {
    checkbox.checked = selectedPlaylistItems.has(Number(checkbox.value));
  }
}

function updatePlaylistSummary() {
  const total = playlistPositions().length;
  const selectedLongVideos = longVideoPositions().filter((position) => selectedPlaylistItems.has(position));
  playlistSummary.textContent = `${selectedPlaylistItems.size} von ${total} Tracks ausgewählt`;
  longVideoWarning.classList.toggle("hidden", selectedLongVideos.length === 0);
  longVideoMessage.textContent = selectedLongVideos.length === 1
    ? "1 ausgewähltes Video ist länger als 10 Minuten."
    : `${selectedLongVideos.length} ausgewählte Videos sind länger als 10 Minuten.`;
  startButton.disabled = total > 0 && selectedPlaylistItems.size === 0;
}

function applyPlaylistMode() {
  const positions = playlistPositions();
  manualActions.classList.toggle("hidden", positions.length === 0);
  selectedPlaylistItems = parseNumberList(playlistRule.value, positions);
  syncCheckboxes();
  updatePlaylistSummary();
}

function currentFormat() {
  return document.querySelector("input[name='format']:checked").value;
}

function isMinimalAudioSelected() {
  return currentFormat() === "mp3" && selectedAudioQuality === "minimal";
}

function syncCoverFieldState() {
  const isPlaylist = currentProbe?.type === "playlist";
  const isMinimalAudio = isMinimalAudioSelected();
  metaCover.disabled = isPlaylist || isMinimalAudio;
  metaCoverLabel.classList.toggle("field-muted", metaCover.disabled);

  if (isMinimalAudio) {
    metaCover.placeholder = "MP3 Minimal nutzt kein Cover";
    metaCover.title = "Bei MP3 Minimal wird kein Cover eingebettet.";
  } else if (isPlaylist) {
    metaCover.placeholder = "Playlist nutzt je Track das jeweilige Video-Cover";
    metaCover.title = "Bei Playlists wird je Track das jeweilige Video-Cover genutzt.";
  } else {
    metaCover.placeholder = "";
    metaCover.title = "";
  }
}

function syncQualityOptions() {
  const format = currentFormat();
  const selectedValue = format === "mp3" ? selectedAudioQuality : selectedVideoQuality;
  qualitySelect.replaceChildren();
  for (const [value, label] of qualityOptions[format]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    qualitySelect.appendChild(option);
  }
  qualitySelect.value = selectedValue;
  syncCoverFieldState();
}

async function postJson(url, payload) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 90000);
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.detail || "Anfrage fehlgeschlagen");
    }
    return data;
  } finally {
    clearTimeout(timeout);
  }
}

function fillProbe(data) {
  currentProbe = data;
  result.classList.remove("hidden");
  resetJobUi();

  thumbnail.src = data.thumbnail || "";
  mediaType.textContent = data.type === "playlist" ? "Playlist" : "Video";
  mediaTitle.textContent = data.title;
  mediaCount.textContent = data.type === "playlist" ? `${data.count} Einträge` : "1 Eintrag";

  const metadata = data.suggested_metadata || {};
  metaTitle.value = metadata.title || "";
  metaArtist.value = metadata.artist || "";
  metaAlbum.value = metadata.album || "";
  metaCover.value = metadata.cover_url || data.thumbnail || "";

  entries.replaceChildren();
  if (data.entries && data.entries.length > 0) {
    playlistDetails.classList.remove("hidden");
    playlistDetails.open = false;
    selectedPlaylistItems = new Set(data.entries.map((item) => item.position));
    syncRuleFromSelection();
    for (const item of data.entries) {
      const li = document.createElement("li");
      const isLongVideo = Number.isFinite(item.duration) && item.duration > longVideoThresholdSeconds;
      li.classList.toggle("long-track", isLongVideo);
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = item.position;
      checkbox.checked = true;
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          selectedPlaylistItems.add(item.position);
        } else {
          selectedPlaylistItems.delete(item.position);
        }
        syncRuleFromSelection();
        syncCheckboxes();
        updatePlaylistSummary();
      });
      const position = document.createElement("span");
      position.className = "track-position";
      position.textContent = `${item.position}.`;
      const title = document.createElement("span");
      title.className = "track-title";
      title.textContent = item.title || "Unbenannter Eintrag";
      label.append(checkbox, position, title);
      if (isLongVideo) {
        const badge = document.createElement("span");
        badge.className = "track-badge";
        badge.textContent = ">10 min";
        label.appendChild(badge);
      }
      li.appendChild(label);
      entries.appendChild(li);
    }
    applyPlaylistMode();
  } else {
    playlistDetails.classList.add("hidden");
    playlistDetails.open = false;
    selectedPlaylistItems = new Set();
    longVideoWarning.classList.add("hidden");
    longVideoMessage.textContent = "";
    updatePlaylistSummary();
  }
  syncCoverFieldState();
}

async function pollJob(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "Job nicht gefunden");
  }

  jobBox.classList.remove("hidden");
  jobMessage.textContent = data.error || data.message;
  jobProgress.value = data.progress;
  jobPercent.textContent = `${Math.round(data.progress)}%`;

  if (data.status === "done") {
    clearInterval(pollTimer);
    pollTimer = null;
    downloadLink.href = `/api/jobs/${jobId}/download`;
    downloadLink.download = data.download_name || "";
    downloadLink.classList.remove("hidden");
    updateDownloadSize(data);
    setNotice(`Fertig. Der Download wird ${data.expires_after_minutes} Minuten bereitgehalten.`);
    startButton.disabled = false;
    refreshHistory();
  }

  if (data.status === "failed") {
    clearInterval(pollTimer);
    pollTimer = null;
    setNotice(data.error || "Download fehlgeschlagen", true);
    startButton.disabled = false;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  result.classList.add("hidden");
  resetJobUi();
  setNotice("Link wird geprüft...");
  probeButton.disabled = true;
  startButton.disabled = false;
  try {
    const data = await postJson("/api/probe", { url: urlInput.value.trim() });
    fillProbe(data);
    setNotice(data.truncated ? "Playlist wurde auf die konfigurierte Maximalanzahl begrenzt." : "");
  } catch (error) {
    const message = error.name === "AbortError"
      ? "Anfrage hat zu lange gedauert. Der Pi/Container erreicht YouTube eventuell nicht."
      : error.message;
    setNotice(message, true);
  } finally {
    probeButton.disabled = false;
  }
});

startButton.addEventListener("click", async () => {
  if (!currentProbe) return;
  resetJobUi();
  setNotice("Job wird gestartet...");
  startButton.disabled = true;

  const format = currentFormat();
  const payload = {
    url: urlInput.value.trim(),
    format,
    audio_quality: selectedAudioQuality,
    video_quality: selectedVideoQuality,
    session_id: sessionId,
    metadata: {
      title: metaTitle.value.trim(),
      artist: metaArtist.value.trim(),
      album: metaAlbum.value.trim(),
      cover_url: metaCover.value.trim(),
    },
  };
  if (currentProbe.type === "playlist") {
    payload.playlist_items = [...selectedPlaylistItems].sort((a, b) => a - b);
  }

  try {
    const data = await postJson("/api/jobs", payload);
    currentJob = data.job_id;
    jobBox.classList.remove("hidden");
    jobProgress.value = 0;
    jobPercent.textContent = "0%";
    jobMessage.textContent = "Wartet auf Start";
    setNotice("");
    pollTimer = setInterval(() => pollJob(currentJob).catch((error) => setNotice(error.message, true)), 1500);
    await pollJob(currentJob);
  } catch (error) {
    setNotice(error.message, true);
    startButton.disabled = false;
  }
});

playlistRule.addEventListener("input", applyPlaylistMode);

for (const input of document.querySelectorAll("input[name='format']")) {
  input.addEventListener("change", syncQualityOptions);
}
qualitySelect.addEventListener("change", () => {
  if (currentFormat() === "mp3") {
    selectedAudioQuality = qualitySelect.value;
  } else {
    selectedVideoQuality = qualitySelect.value;
  }
  syncCoverFieldState();
  saveQualityPreferences();
});
loadQualityPreferences();
syncQualityOptions();

selectAllButton.addEventListener("click", () => {
  selectedPlaylistItems = new Set(playlistPositions());
  syncRuleFromSelection();
  syncCheckboxes();
  updatePlaylistSummary();
});

selectNoneButton.addEventListener("click", () => {
  selectedPlaylistItems = new Set();
  syncRuleFromSelection();
  syncCheckboxes();
  updatePlaylistSummary();
});

selectInvertButton.addEventListener("click", () => {
  selectedPlaylistItems = new Set(playlistPositions().filter((position) => !selectedPlaylistItems.has(position)));
  syncRuleFromSelection();
  syncCheckboxes();
  updatePlaylistSummary();
});

excludeLongVideosButton.addEventListener("click", () => {
  const longVideos = new Set(longVideoPositions());
  selectedPlaylistItems = new Set([...selectedPlaylistItems].filter((position) => !longVideos.has(position)));
  syncRuleFromSelection();
  syncCheckboxes();
  updatePlaylistSummary();
});

historyToggle.addEventListener("click", () => {
  const isOpen = !historyPanel.classList.contains("hidden");
  historyPanel.classList.toggle("hidden", isOpen);
  historyToggle.setAttribute("aria-expanded", String(!isOpen));
  if (isOpen) return;
  refreshHistory();
});

historyRefresh.addEventListener("click", refreshHistory);

document.addEventListener("click", (event) => {
  if (historyPanel.classList.contains("hidden")) return;
  if (event.target.closest(".temp-history")) return;
  historyPanel.classList.add("hidden");
  historyToggle.setAttribute("aria-expanded", "false");
});

setInterval(refreshHistory, 30000);
refreshHistory();
