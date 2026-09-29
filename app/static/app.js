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
const jobPhase = document.querySelector("#job-phase");
const jobMessage = document.querySelector("#job-message");
const jobPercent = document.querySelector("#job-percent");
const jobProgress = document.querySelector("#job-progress");
const downloadLink = document.querySelector("#download-link");
const downloadSize = document.querySelector("#download-size");
const metadataReview = document.querySelector("#metadata-review");
const reviewSummary = document.querySelector("#review-summary");
const reviewList = document.querySelector("#review-list");
const finalizeButton = document.querySelector("#finalize-button");
const coverToggle = document.querySelector("#cover-toggle");
const coverInput = document.querySelector("#minimal-cover");
const historyToggle = document.querySelector("#history-toggle");
const historyCount = document.querySelector("#history-count");
const historyPanel = document.querySelector("#history-panel");
const historyRefresh = document.querySelector("#history-refresh");
const historyEmpty = document.querySelector("#history-empty");
const historyList = document.querySelector("#history-list");
const qualitySelect = document.querySelector("#quality-select");

let currentProbe = null;
let currentJob = null;
let pollTimer = null;
let selectedPlaylistItems = new Set();
let selectedAudioQuality = "medium";
let selectedVideoQuality = "best_compatible";
let embedCover = true;
const longVideoThresholdSeconds = 10 * 60;
const qualityPreferenceCookie = "yt2mpx-quality-preferences";

const qualityOptions = {
  mp3: [
    ["high", "High (VBR 0)"],
    ["medium", "Medium (192 kbps)"],
    ["small", "Low (128 kbps)"],
    ["minimal", "Minimal (32 kbps, smallest source)"],
  ],
  mp4: [
    ["best_compatible", "Best compatibility"],
    ["1080", "1080p"],
    ["720", "720p"],
    ["360", "360p"],
  ],
};

const phaseLabels = {
  download: "Download",
  convert: "Conversion",
  fingerprint: "Fingerprint",
  musicbrainz: "MusicBrainz",
  review: "Metadata review",
  finalize: "Preparing files",
  done: "Ready",
  queued: "Waiting",
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
    if (preferences.audio === "minimal_cover") {
      selectedAudioQuality = "minimal";
      embedCover = true;
    } else if (isValidQuality("mp3", preferences.audio)) {
      selectedAudioQuality = preferences.audio;
    }
    if (isValidQuality("mp4", preferences.video)) {
      selectedVideoQuality = preferences.video;
    }
    if (typeof preferences.minimalCover === "boolean") {
      embedCover = preferences.minimalCover;
    }
    if (typeof preferences.embedCover === "boolean") {
      embedCover = preferences.embedCover;
    }
  } catch {
    document.cookie = `${qualityPreferenceCookie}=; path=/; max-age=0; SameSite=Lax`;
  }
}

function saveQualityPreferences() {
  const preferences = encodeURIComponent(JSON.stringify({
    audio: selectedAudioQuality,
    video: selectedVideoQuality,
    embedCover,
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
  metadataReview.classList.add("hidden");
  reviewList.replaceChildren();
  reviewSummary.textContent = "";
  finalizeButton.disabled = false;
  downloadLink.classList.add("hidden");
  downloadLink.removeAttribute("href");
  downloadLink.removeAttribute("download");
  downloadSize.classList.add("hidden");
  downloadSize.textContent = "";
  jobProgress.value = 0;
  jobPercent.textContent = "0%";
  jobPhase.textContent = "Waiting";
  jobMessage.textContent = "Waiting";
  updateJobProgress({ status: "queued", progress: 0, message: "Waiting" });
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
  if (!Number.isFinite(seconds) || seconds <= 0) return "expires soon";
  const minutes = Math.max(1, Math.ceil(seconds / 60));
  return minutes === 1 ? "1 min left" : `${minutes} min left`;
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
    ? `Download: ${downloadText} ZIP, about ${unpackedText} extracted`
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
    if (!response.ok) throw new Error(data.detail || "Download history is unavailable");
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
  playlistSummary.textContent = `${selectedPlaylistItems.size} of ${total} tracks selected`;
  longVideoWarning.classList.toggle("hidden", selectedLongVideos.length === 0);
  longVideoMessage.textContent = selectedLongVideos.length === 1
    ? "1 selected video is longer than 10 minutes."
    : `${selectedLongVideos.length} selected videos are longer than 10 minutes.`;
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
  syncCoverToggle();
}

function syncCoverToggle() {
  const showToggle = currentFormat() === "mp3";
  coverToggle.classList.toggle("hidden", !showToggle);
  coverInput.checked = embedCover;
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
      throw new Error(data.detail || "Request failed");
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
  mediaCount.textContent = data.type === "playlist" ? `${data.count} items` : "1 item";

  entries.replaceChildren();
  if (data.entries && data.entries.length > 0) {
    playlistDetails.classList.remove("hidden");
    playlistDetails.open = true;
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
      title.textContent = item.title || "Untitled item";
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
}

function phaseForJob(data) {
  if (data.status === "done") return "done";
  if (data.status === "finalizing") return "finalize";
  if (data.status === "review") return "review";
  if (data.progress >= 78) return "musicbrainz";
  if (data.progress >= 68) return "fingerprint";
  if (data.progress >= 60) return "convert";
  return "download";
}

function phaseProgress(data, phase) {
  const progress = Number(data.progress) || 0;
  if (phase === "download") {
    return Math.max(0, Math.min(100, ((progress - 1) / 59) * 100));
  }
  if (phase === "convert") {
    return Math.max(0, Math.min(100, ((progress - 60) / 8) * 100));
  }
  if (phase === "fingerprint" || phase === "musicbrainz") {
    return Math.max(0, Math.min(100, ((progress - 68) / 23) * 100));
  }
  if (phase === "review") {
    return 100;
  }
  if (phase === "finalize") {
    return Math.max(0, Math.min(100, ((progress - 92) / 8) * 100));
  }
  if (phase === "done") {
    return 100;
  }
  return 0;
}

function updateJobProgress(data) {
  const activePhase = phaseForJob(data);
  const percent = Math.round(phaseProgress(data, activePhase));
  jobPhase.textContent = phaseLabels[activePhase] || phaseLabels.queued;
  jobProgress.value = percent;
  jobPercent.textContent = `${percent}%`;
  jobMessage.textContent = data.error || data.message || "";
}

function fieldValue(metadata, field) {
  return metadata?.[field] || "";
}

function makeInput(field, label, value, type = "text") {
  const wrapper = document.createElement("label");
  wrapper.textContent = label;
  const input = document.createElement("input");
  input.type = type;
  input.dataset.field = field;
  input.value = value || "";
  wrapper.appendChild(input);
  return wrapper;
}

function renderReview(data) {
  const tracks = data.tracks || [];
  const matched = tracks.filter((track) => track.status === "matched").length;
  metadataReview.classList.remove("hidden");
  reviewSummary.textContent = `${tracks.length} track${tracks.length === 1 ? "" : "s"} ready; ${matched} matched in MusicBrainz.`;
  reviewList.replaceChildren();

  for (const track of tracks) {
    const metadata = track.metadata || {};
    const article = document.createElement("article");
    article.className = "review-card";
    article.dataset.trackId = track.id;

    const head = document.createElement("div");
    head.className = "review-card-head";
    const title = document.createElement("strong");
    title.textContent = `${track.position}. ${fieldValue(metadata, "title") || track.source_title || "Untitled track"}`;
    const status = document.createElement("span");
    status.className = track.status === "matched" ? "match-pill matched" : "match-pill fallback";
    status.textContent = track.status === "matched"
      ? `Matched ${Math.round((track.confidence || 0) * 100)}%`
      : "Fallback";
    head.append(title, status);

    const source = document.createElement("p");
    source.className = "review-source";
    source.textContent = track.message || track.source_title || track.file_name || "";

    const mainFields = document.createElement("div");
    mainFields.className = "review-fields";
    mainFields.append(
      makeInput("title", "Title", fieldValue(metadata, "title")),
      makeInput("artist", "Artist", fieldValue(metadata, "artist")),
      makeInput("album", "Album", fieldValue(metadata, "album")),
    );

    const details = document.createElement("details");
    details.className = "review-details";
    const summary = document.createElement("summary");
    summary.textContent = "More tags";
    const extraFields = document.createElement("div");
    extraFields.className = "review-fields extra";
    extraFields.append(
      makeInput("date", "Date / year", fieldValue(metadata, "date")),
      makeInput("track_number", "Track", fieldValue(metadata, "track_number")),
      makeInput("disc_number", "Disc", fieldValue(metadata, "disc_number")),
      makeInput("isrc", "ISRC", fieldValue(metadata, "isrc")),
      makeInput("cover_url", "Cover URL", fieldValue(metadata, "cover_url"), "url"),
      makeInput("musicbrainz_recording_id", "MB Recording ID", fieldValue(metadata, "musicbrainz_recording_id")),
      makeInput("musicbrainz_release_id", "MB Release ID", fieldValue(metadata, "musicbrainz_release_id")),
      makeInput("musicbrainz_release_group_id", "MB Release Group ID", fieldValue(metadata, "musicbrainz_release_group_id")),
    );
    details.append(summary, extraFields);

    article.append(head, source, mainFields, details);
    reviewList.appendChild(article);
  }
}

function collectReviewTracks() {
  return [...reviewList.querySelectorAll(".review-card")].map((card) => {
    const metadata = {};
    for (const input of card.querySelectorAll("input[data-field]")) {
      metadata[input.dataset.field] = input.value.trim();
    }
    return {
      id: card.dataset.trackId,
      metadata,
    };
  });
}

async function pollJob(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "Job not found");
  }

  jobBox.classList.remove("hidden");
  updateJobProgress(data);

  if (data.status === "review") {
    clearInterval(pollTimer);
    pollTimer = null;
    renderReview(data);
    setNotice("Metadata is ready. Review it, then prepare your download.");
    startButton.disabled = false;
    metadataReview.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  if (data.status === "done") {
    clearInterval(pollTimer);
    pollTimer = null;
    metadataReview.classList.add("hidden");
    downloadLink.href = `/api/jobs/${jobId}/download`;
    downloadLink.download = data.download_name || "";
    downloadLink.classList.remove("hidden");
    updateDownloadSize(data);
    setNotice(`Ready. This download will remain available for ${data.expires_after_minutes} minutes.`);
    startButton.disabled = false;
    finalizeButton.disabled = false;
    refreshHistory();
  }

  if (data.status === "failed") {
    clearInterval(pollTimer);
    pollTimer = null;
    setNotice(data.error || "Download failed", true);
    startButton.disabled = false;
    finalizeButton.disabled = false;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  result.classList.add("hidden");
  resetJobUi();
  setNotice("Checking link...");
  probeButton.disabled = true;
  startButton.disabled = false;
  try {
    const data = await postJson("/api/probe", { url: urlInput.value.trim() });
    fillProbe(data);
    setNotice(data.truncated ? "The playlist was limited to the configured maximum number of items." : "");
  } catch (error) {
    const message = error.name === "AbortError"
      ? "The request timed out. The Pi or container may be unable to reach YouTube."
      : error.message;
    setNotice(message, true);
  } finally {
    probeButton.disabled = false;
  }
});

startButton.addEventListener("click", async () => {
  if (!currentProbe) return;
  resetJobUi();
  setNotice("Starting download and metadata lookup...");
  startButton.disabled = true;
  playlistDetails.open = false;

  const format = currentFormat();
  const payload = {
    url: urlInput.value.trim(),
    format,
    audio_quality: selectedAudioQuality,
    embed_cover: embedCover,
    video_quality: selectedVideoQuality,
    session_id: sessionId,
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
    jobPhase.textContent = "Download";
    jobMessage.textContent = "Waiting to start";
    setNotice("");
    pollTimer = setInterval(() => pollJob(currentJob).catch((error) => setNotice(error.message, true)), 1500);
    await pollJob(currentJob);
  } catch (error) {
    setNotice(error.message, true);
    startButton.disabled = false;
  }
});

finalizeButton.addEventListener("click", async () => {
  if (!currentJob) return;
  finalizeButton.disabled = true;
  startButton.disabled = true;
  setNotice("Writing tags and preparing the download...");
  try {
    await postJson(`/api/jobs/${currentJob}/finalize`, { tracks: collectReviewTracks() });
    metadataReview.classList.add("hidden");
    pollTimer = setInterval(() => pollJob(currentJob).catch((error) => setNotice(error.message, true)), 1500);
    await pollJob(currentJob);
  } catch (error) {
    setNotice(error.message, true);
    finalizeButton.disabled = false;
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
  syncCoverToggle();
  saveQualityPreferences();
});
coverInput.addEventListener("change", () => {
  embedCover = coverInput.checked;
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
