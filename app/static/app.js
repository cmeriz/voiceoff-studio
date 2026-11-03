"use strict";

const POLL_INTERVAL_MS = 2000;
const ALLOWED_EXTENSIONS = [".mp3", ".wav"];
const PENDING_STATUSES = new Set(["queued", "processing"]);
const SEPARATION_END_PERCENT = 95;
const MIN_PERCENT_FOR_ESTIMATE = 3;

const elements = {
  dropzone: document.getElementById("dropzone"),
  fileInput: document.getElementById("file-input"),
  limits: document.getElementById("limits"),
  uploadError: document.getElementById("upload-error"),
  section: document.getElementById("tracks-section"),
  list: document.getElementById("track-list"),
  template: document.getElementById("track-template"),
};

let config = { max_upload_mb: 100, max_duration_seconds: 900, retention_hours: 24 };
let pollTimer = null;
const cards = new Map(); // job id or local upload id -> card
let uploadQueue = Promise.resolve();
let nextUploadId = 0;
let activePlayer = null;

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}

function formatRemaining(seconds) {
  const minutes = Math.ceil(seconds / 60);
  return minutes <= 1 ? "queda menos de 1 min" : `quedan unos ${minutes} min`;
}

function formatExpiry(seconds) {
  if (seconds >= 3600) return `Se borra en ${Math.ceil(seconds / 3600)} h`;
  return `Se borra en ${Math.max(Math.ceil(seconds / 60), 1)} min`;
}

function showUploadError(message) {
  elements.uploadError.textContent = message;
  elements.uploadError.hidden = !message;
}

function createCard(key, filename) {
  const node = elements.template.content.firstElementChild.cloneNode(true);
  const card = {
    key,
    node,
    name: node.querySelector(".track-name"),
    meta: node.querySelector(".track-meta"),
    remove: node.querySelector(".track-remove"),
    progress: node.querySelector(".track-progress"),
    bar: node.querySelector(".progress-bar"),
    barFill: node.querySelector(".progress-bar span"),
    label: node.querySelector(".progress-label"),
    error: node.querySelector(".track-error"),
    playerNode: node.querySelector(".player"),
    player: null,
    onRemove: null,
  };
  card.name.textContent = filename;
  card.name.title = filename;
  card.remove.setAttribute("aria-label", `Quitar ${filename}`);
  card.remove.title = "Quitar";
  card.remove.addEventListener("click", () => card.onRemove?.());
  cards.set(key, card);
  elements.list.prepend(node);
  elements.section.hidden = false;
  return card;
}

function removeCard(card) {
  if (card.player) card.player.destroy();
  card.node.remove();
  cards.delete(card.key);
  elements.section.hidden = cards.size === 0;
}

function setProgress(card, { percent, label }) {
  card.progress.hidden = false;
  card.label.textContent = label;
  if (percent === null) {
    card.bar.classList.add("is-waiting");
    card.bar.removeAttribute("aria-valuenow");
    card.barFill.style.width = "";
  } else {
    card.bar.classList.remove("is-waiting");
    card.bar.setAttribute("aria-valuenow", String(percent));
    card.barFill.style.width = `${percent}%`;
  }
}

function describeProcessing(job) {
  if (job.progress === 0) return "Preparando el modelo de IA…";
  if (job.progress >= SEPARATION_END_PERCENT) return "Generando el archivo final…";
  let label = `Separando la voz · ${job.progress} %`;
  if (job.elapsed_seconds && job.progress >= MIN_PERCENT_FOR_ESTIMATE) {
    const remaining = job.elapsed_seconds * (SEPARATION_END_PERCENT - job.progress) / job.progress;
    label += ` · ${formatRemaining(remaining)}`;
  }
  return label;
}

function renderJob(job) {
  const card = cards.get(job.id) ?? createCard(job.id, job.filename);
  card.onRemove = () => deleteJob(job.id, card);

  const parts = [job.format.toUpperCase(), formatTime(job.duration)];
  if (job.status === "done" && job.expires_in_seconds !== null) {
    parts.push(formatExpiry(job.expires_in_seconds));
  }
  card.meta.textContent = parts.join(" · ");

  if (job.status === "queued") {
    setProgress(card, { percent: null, label: "En cola. Se procesa una canción a la vez." });
  } else if (job.status === "processing") {
    const percent = job.progress === 0 ? null : job.progress;
    setProgress(card, { percent, label: describeProcessing(job) });
  } else {
    card.progress.hidden = true;
  }

  card.error.hidden = job.status !== "failed";
  card.error.textContent = job.status === "failed" ? job.error : "";

  if (job.status === "done" && !card.player) {
    card.player = new Player(card.playerNode, job);
    card.playerNode.hidden = false;
  }
}

async function readError(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // The response was not JSON; use the generic message.
  }
  return "No se pudo completar la operación. Inténtalo de nuevo.";
}

async function deleteJob(jobId, card) {
  card.remove.disabled = true;
  try {
    const response = await fetch(`/api/jobs/${jobId}`, { method: "DELETE" });
    if (!response.ok && response.status !== 404) {
      showUploadError(await readError(response));
      return;
    }
    removeCard(card);
  } catch {
    showUploadError("No se pudo conectar con el servidor.");
  } finally {
    card.remove.disabled = false;
  }
}

async function refreshJobs() {
  clearTimeout(pollTimer);
  let hasPending = false;
  try {
    const response = await fetch("/api/jobs", { cache: "no-store" });
    if (response.ok) {
      const jobs = await response.json();
      const ids = new Set(jobs.map((job) => job.id));
      for (const [key, card] of cards) {
        if (!key.startsWith("upload-") && !ids.has(key)) removeCard(card);
      }
      // The API lists newest first; prepending oldest first keeps that order.
      for (const job of [...jobs].reverse()) renderJob(job);
      hasPending = jobs.some((job) => PENDING_STATUSES.has(job.status));
    }
  } catch {
    hasPending = true; // Server unreachable for now: keep trying.
  }
  if (hasPending) pollTimer = setTimeout(refreshJobs, POLL_INTERVAL_MS);
}

function validateFile(file) {
  const name = file.name.toLowerCase();
  if (!ALLOWED_EXTENSIONS.some((extension) => name.endsWith(extension))) {
    return `«${file.name}» no es un archivo MP3 o WAV.`;
  }
  if (file.size === 0) return `«${file.name}» está vacío.`;
  if (file.size > config.max_upload_mb * 1024 * 1024) {
    return `«${file.name}» supera el máximo de ${config.max_upload_mb} MB.`;
  }
  return null;
}

function uploadFile(file) {
  return new Promise((resolve) => {
    const card = createCard(`upload-${nextUploadId++}`, file.name);
    const request = new XMLHttpRequest();
    setProgress(card, { percent: 0, label: "Subiendo · 0 %" });
    card.onRemove = () => request.abort();

    request.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.round((event.loaded / event.total) * 100);
      if (percent === 100) {
        setProgress(card, { percent: null, label: "Comprobando el audio…" });
      } else {
        setProgress(card, { percent, label: `Subiendo · ${percent} %` });
      }
    });
    request.addEventListener("load", async () => {
      removeCard(card);
      if (request.status === 201) {
        await refreshJobs();
      } else {
        let message = "No se pudo subir el archivo.";
        try {
          message = JSON.parse(request.responseText).detail ?? message;
        } catch {
          // Keep the generic message.
        }
        showUploadError(`«${file.name}»: ${message}`);
      }
      resolve();
    });
    request.addEventListener("error", () => {
      removeCard(card);
      showUploadError(`No se pudo subir «${file.name}». Comprueba la conexión.`);
      resolve();
    });
    request.addEventListener("abort", () => {
      removeCard(card);
      resolve();
    });

    request.open("POST", `/api/jobs?filename=${encodeURIComponent(file.name)}`);
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.send(file);
  });
}

function handleFiles(fileList) {
  showUploadError("");
  const errors = [];
  for (const file of fileList) {
    const error = validateFile(file);
    if (error) {
      errors.push(error);
    } else {
      uploadQueue = uploadQueue.then(() => uploadFile(file));
    }
  }
  if (errors.length) showUploadError(errors.join(" "));
}

/**
 * Plays one of two audio elements at a time. Flipping the voice switch moves
 * playback to the other element at the same position.
 */
class Player {
  constructor(root, job) {
    this.root = root;
    this.playButton = root.querySelector(".play-button");
    this.iconPlay = root.querySelector(".icon-play");
    this.iconPause = root.querySelector(".icon-pause");
    this.seek = root.querySelector(".seek");
    this.current = root.querySelector(".time-current");
    this.total = root.querySelector(".time-total");
    this.toggle = root.querySelector(".voice-toggle");
    this.toggleState = root.querySelector(".voice-toggle-state");
    this.karaoke = this.createAudio(`/api/jobs/${job.id}/karaoke`);
    this.original = this.createAudio(`/api/jobs/${job.id}/original`);
    this.audio = this.karaoke;
    this.seeking = false;

    this.total.textContent = formatTime(job.duration);
    const download = root.querySelector(".download-button");
    download.href = `/api/jobs/${job.id}/karaoke?download=true`;
    download.setAttribute("download", "");

    this.playButton.addEventListener("click", () => this.togglePlay());
    this.toggle.addEventListener("click", () => this.toggleVoice());
    this.seek.addEventListener("input", () => {
      this.seeking = true;
      this.current.textContent = formatTime(this.seekTime());
      this.paintSeek();
    });
    this.seek.addEventListener("change", () => {
      this.audio.currentTime = this.seekTime();
      this.seeking = false;
    });
  }

  createAudio(src) {
    const audio = new Audio();
    audio.preload = "metadata";
    audio.src = src;
    audio.addEventListener("timeupdate", () => { if (audio === this.audio) this.syncTimeline(); });
    audio.addEventListener("loadedmetadata", () => { if (audio === this.audio) this.syncTimeline(); });
    audio.addEventListener("play", () => { if (audio === this.audio) this.setPlaying(true); });
    audio.addEventListener("pause", () => { if (audio === this.audio) this.setPlaying(false); });
    audio.addEventListener("ended", () => {
      if (audio === this.audio) {
        audio.currentTime = 0;
        this.syncTimeline();
      }
    });
    return audio;
  }

  seekTime() {
    return (Number(this.seek.value) / Number(this.seek.max)) * (this.audio.duration || 0);
  }

  paintSeek() {
    this.seek.style.setProperty("--played", `${(Number(this.seek.value) / Number(this.seek.max)) * 100}%`);
  }

  syncTimeline() {
    if (this.seeking) return;
    const { currentTime, duration } = this.audio;
    if (duration) {
      this.seek.value = String(Math.round((currentTime / duration) * Number(this.seek.max)));
      this.total.textContent = formatTime(duration);
    }
    this.current.textContent = formatTime(currentTime);
    this.paintSeek();
  }

  setPlaying(playing) {
    this.iconPlay.toggleAttribute("hidden", playing);
    this.iconPause.toggleAttribute("hidden", !playing);
    this.playButton.setAttribute("aria-label", playing ? "Pausar" : "Reproducir");
  }

  async togglePlay() {
    if (!this.audio.paused) {
      this.audio.pause();
      return;
    }
    if (activePlayer && activePlayer !== this) activePlayer.audio.pause();
    activePlayer = this;
    try {
      await this.audio.play();
    } catch (error) {
      if (error.name !== "AbortError") showUploadError("No se pudo reproducir el audio.");
    }
  }

  toggleVoice() {
    const voiceOn = this.audio === this.karaoke;
    const previous = this.audio;
    const wasPlaying = !previous.paused;
    const position = previous.currentTime;
    this.audio = voiceOn ? this.original : this.karaoke;
    previous.pause();

    const resume = () => {
      this.audio.currentTime = position;
      if (wasPlaying) {
        this.audio.play().catch(() => this.setPlaying(false));
      }
    };
    if (this.audio.readyState >= HTMLMediaElement.HAVE_METADATA) {
      resume();
    } else {
      this.audio.addEventListener("loadedmetadata", resume, { once: true });
      this.audio.load();
    }

    this.setPlaying(wasPlaying);
    this.toggle.setAttribute("aria-checked", String(voiceOn));
    this.toggleState.textContent = voiceOn ? "On" : "Off";
  }

  destroy() {
    for (const audio of [this.karaoke, this.original]) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }
    if (activePlayer === this) activePlayer = null;
  }
}

function setupDropzone() {
  const { dropzone, fileInput } = elements;
  fileInput.addEventListener("change", () => {
    handleFiles(fileInput.files);
    fileInput.value = "";
  });
  for (const type of ["dragenter", "dragover"]) {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.add("is-dragging");
    });
  }
  dropzone.addEventListener("dragleave", (event) => {
    if (!dropzone.contains(event.relatedTarget)) dropzone.classList.remove("is-dragging");
  });
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-dragging");
    handleFiles(event.dataTransfer.files);
  });
  // A file dropped outside the dropzone must not navigate away from the page.
  for (const type of ["dragover", "drop"]) {
    window.addEventListener(type, (event) => event.preventDefault());
  }
}

async function loadConfig() {
  try {
    const response = await fetch("/api/config");
    if (response.ok) config = await response.json();
  } catch {
    // Keep the defaults; the server still enforces the real limits.
  }
  const minutes = Math.floor(config.max_duration_seconds / 60);
  elements.limits.textContent = `MP3 o WAV · hasta ${config.max_upload_mb} MB y ${minutes} min`;
  document.getElementById("privacy-note").textContent =
    `Todo se procesa en este equipo. Tus archivos se borran a las ${config.retention_hours} h.`;
}

setupDropzone();
loadConfig();
refreshJobs();
