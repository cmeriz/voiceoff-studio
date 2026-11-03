"use strict";

const POLL_INTERVAL_MS = 2000;
const ALLOWED_EXTENSIONS = [".mp3", ".wav"];
const PENDING_STATUSES = new Set(["queued", "processing"]);
const SEPARATION_END_PERCENT = 95;
const MIN_PERCENT_FOR_ESTIMATE = 3;
const PITCH_DEBOUNCE_MS = 350;
const PITCH_SECONDS_PER_AUDIO_SECOND = 0.1;
const MIN_PITCH_ESTIMATE_SECONDS = 5;

const elements = {
  dropzone: document.getElementById("dropzone"),
  fileInput: document.getElementById("file-input"),
  limits: document.getElementById("limits"),
  uploadError: document.getElementById("upload-error"),
  section: document.getElementById("tracks-section"),
  list: document.getElementById("track-list"),
  template: document.getElementById("track-template"),
  confirmDialog: document.getElementById("confirm-dialog"),
  confirmTitle: document.getElementById("confirm-title"),
  confirmMessage: document.getElementById("confirm-message"),
  confirmCancel: document.getElementById("confirm-cancel"),
  confirmAccept: document.getElementById("confirm-accept"),
  clearPitch: document.getElementById("clear-pitch"),
  deleteAll: document.getElementById("delete-all"),
};

let config = { max_upload_mb: 100, max_duration_seconds: 900, retention_hours: 24, max_semitones: 6 };
let pollTimer = null;
const cards = new Map(); // job id or local upload id -> card
let uploadQueue = Promise.resolve();
let nextUploadId = 0;
let activePlayer = null;
let confirmation = null; // The request the open dialog is asking about.
let latestJobs = [];
let bulkActionRunning = false;
const activeUploads = new Set();
let uploadsPending = 0; // Selected files not finished yet, including those still waiting their turn.
let uploadBatch = 0; // Increases when uploads are cancelled, so waiting files know to skip.

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

function formatSemitones(semitones) {
  if (semitones > 0) return `+${semitones}`;
  return semitones < 0 ? `−${-semitones}` : "0";
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

function askConfirmation(card, { title, message, acceptLabel, cancelLabel }, onAccept) {
  const { confirmDialog, confirmTitle, confirmMessage, confirmAccept, confirmCancel } = elements;
  confirmTitle.textContent = title;
  confirmMessage.textContent = message;
  confirmAccept.textContent = acceptLabel;
  confirmCancel.textContent = cancelLabel;
  confirmDialog.returnValue = "";
  confirmation = { card, onAccept };
  confirmDialog.showModal();
}

function describeDeletion(job) {
  const files = "el audio original, el karaoke y sus versiones de tono";
  let message = `Se borrarán del servidor ${files}. No se puede deshacer.`;
  if (job.status === "processing") {
    message = `Se detendrá el procesamiento y se borrará el audio del servidor. No se puede deshacer.`;
  } else if (job.status === "queued") {
    message = "Saldrá de la cola y se borrará el audio del servidor. No se puede deshacer.";
  } else if (job.status === "failed") {
    message = "Se borrará el audio del servidor. No se puede deshacer.";
  }
  return {
    title: `¿Eliminar «${job.filename}»?`,
    message,
    acceptLabel: "Eliminar",
    cancelLabel: "Cancelar",
  };
}

function removeCard(card) {
  // The card can vanish while the dialog is open, such as when its upload finishes.
  if (confirmation?.card === card) elements.confirmDialog.close();
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
  card.onRemove = () => askConfirmation(card, describeDeletion(job), () => deleteJob(job.id, card));

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
  card.player?.update(job);
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
    refreshJobs();
  } catch {
    showUploadError("No se pudo conectar con el servidor.");
  } finally {
    card.remove.disabled = false;
  }
}

function countPitchVersions() {
  return latestJobs
    .flatMap((job) => Object.values(job.pitch_versions))
    .filter((status) => status === "done" || status === "processing")
    .length;
}

function renderBulkActions() {
  const { clearPitch, deleteAll } = elements;
  const pitchCount = countPitchVersions();
  clearPitch.textContent = `Borrar cambios de tono (${pitchCount})`;
  clearPitch.disabled = bulkActionRunning || pitchCount === 0;
  deleteAll.textContent = `Eliminar todo (${latestJobs.length})`;
  deleteAll.disabled = bulkActionRunning || latestJobs.length + uploadsPending === 0;
}

async function runBulkAction(action) {
  bulkActionRunning = true;
  renderBulkActions();
  showUploadError("");
  try {
    const response = await action();
    if (!response.ok) showUploadError(await readError(response));
  } catch {
    showUploadError("No se pudo conectar con el servidor.");
  } finally {
    bulkActionRunning = false;
    await refreshJobs();
    renderBulkActions();
  }
}

async function clearPitchVersions() {
  await runBulkAction(async () => {
    const response = await fetch("/api/pitch-versions", { method: "DELETE" });
    if (response.ok) {
      for (const card of cards.values()) card.player?.resetPitch();
    }
    return response;
  });
}

async function deleteAllTracks() {
  uploadBatch += 1;
  for (const request of activeUploads) request.abort();
  await runBulkAction(() => fetch("/api/jobs", { method: "DELETE" }));
}

async function askToClearPitchVersions() {
  await refreshJobs();
  const count = countPitchVersions();
  if (count === 0) return;
  askConfirmation(null, {
    title: count === 1 ? "¿Borrar 1 cambio de tono?" : `¿Borrar los ${count} cambios de tono?`,
    message: "Las canciones y sus karaokes en el tono original se conservan. Podrás volver a generar cualquier tono.",
    acceptLabel: "Borrar tonos",
    cancelLabel: "Cancelar",
  }, clearPitchVersions);
}

async function askToDeleteAll() {
  await refreshJobs();
  const count = latestJobs.length;
  if (count + uploadsPending === 0) return;
  const parts = [];
  if (count > 0) {
    parts.push("Se borrarán del servidor los audios originales, los karaokes y todos los cambios de tono.");
  }
  if (latestJobs.some((job) => PENDING_STATUSES.has(job.status))) {
    parts.push("Se detendrá lo que esté en proceso o en cola.");
  }
  if (uploadsPending > 0) parts.push("Se cancelarán las subidas en curso.");
  if (count > 0) parts.push("No se puede deshacer: descarga antes lo que quieras conservar.");
  const onlyUploads = count === 0;
  let title = `¿Eliminar las ${count} pistas?`;
  if (onlyUploads) title = "¿Cancelar todas las subidas?";
  else if (count === 1) title = "¿Eliminar la única pista?";
  let acceptLabel = `Eliminar ${count}`;
  if (onlyUploads) acceptLabel = "Cancelar subidas";
  askConfirmation(null, {
    title,
    message: parts.join(" "),
    acceptLabel,
    cancelLabel: onlyUploads ? "Seguir subiendo" : "Cancelar",
  }, deleteAllTracks);
}

async function refreshJobs() {
  clearTimeout(pollTimer);
  let hasPending = false;
  try {
    const response = await fetch("/api/jobs", { cache: "no-store" });
    if (response.ok) {
      const jobs = await response.json();
      latestJobs = jobs;
      const ids = new Set(jobs.map((job) => job.id));
      for (const [key, card] of cards) {
        if (!key.startsWith("upload-") && !ids.has(key)) removeCard(card);
      }
      // The API lists newest first; prepending oldest first keeps that order.
      for (const job of [...jobs].reverse()) renderJob(job);
      hasPending = jobs.some((job) =>
        PENDING_STATUSES.has(job.status) || Object.values(job.pitch_versions).includes("processing"));
    }
  } catch {
    hasPending = true; // Server unreachable for now: keep trying.
  }
  renderBulkActions();
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
    activeUploads.add(request);
    request.addEventListener("loadend", () => activeUploads.delete(request));
    setProgress(card, { percent: 0, label: "Subiendo · 0 %" });
    card.onRemove = () => askConfirmation(card, {
      title: `¿Cancelar la subida de «${file.name}»?`,
      message: "El archivo no se procesará. Tu copia local no se ve afectada.",
      acceptLabel: "Cancelar subida",
      cancelLabel: "Seguir subiendo",
    }, () => request.abort());

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
      const batch = uploadBatch;
      uploadsPending += 1;
      uploadQueue = uploadQueue
        .then(() => (batch === uploadBatch ? uploadFile(file) : null))
        .then(() => {
          uploadsPending -= 1;
          renderBulkActions();
        });
    }
  }
  renderBulkActions();
  if (errors.length) showUploadError(errors.join(" "));
}

/**
 * Plays one of two audio elements at a time. Flipping the voice switch moves
 * playback to the other element at the same position. Changing the pitch swaps
 * the karaoke element's source once the server has that version ready, and the
 * previous tone keeps playing until then.
 */
class Player {
  constructor(root, job) {
    this.root = root;
    this.job = job;
    this.selectedSemitones = 0;
    this.playingSemitones = 0;
    this.pitchWaiting = false;
    this.pitchError = "";
    this.pitchTimer = null;
    this.pendingSwap = null;
    this.pitchDown = root.querySelector(".pitch-down");
    this.pitchUp = root.querySelector(".pitch-up");
    this.pitchValue = root.querySelector(".pitch-value");
    this.note = root.querySelector(".player-note");
    this.download = root.querySelector(".download-button");
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
    this.download.setAttribute("download", "");
    this.renderPitch();

    this.playButton.addEventListener("click", () => this.togglePlay());
    this.toggle.addEventListener("click", () => this.toggleVoice());
    this.pitchDown.addEventListener("click", () => this.changePitch(-1));
    this.pitchUp.addEventListener("click", () => this.changePitch(1));
    this.seek.addEventListener("input", () => {
      this.seeking = true;
      this.current.textContent = formatTime(this.seekTime());
      this.paintSeek();
    });
    this.seek.addEventListener("change", () => {
      if (this.pendingSwap) {
        this.pendingSwap.position = this.seekTime();
      } else {
        this.audio.currentTime = this.seekTime();
      }
      this.seeking = false;
    });
  }

  update(job) {
    this.job = job;
    // While the debounce runs, the status shown is for a request not yet made.
    if (!this.pitchWaiting || this.pitchTimer !== null) return;
    const status = job.pitch_versions[this.selectedSemitones];
    if (status === "done") {
      this.switchKaraoke(this.selectedSemitones);
    } else if (status === "failed") {
      this.failPitch();
    }
  }

  changePitch(delta) {
    const limit = config.max_semitones;
    this.selectedSemitones = Math.min(limit, Math.max(-limit, this.selectedSemitones + delta));
    this.pitchError = "";
    if (this.audio === this.original) this.toggleVoice();
    this.renderPitch();
    clearTimeout(this.pitchTimer);
    this.pitchTimer = setTimeout(() => {
      this.pitchTimer = null;
      this.applyPitch();
    }, PITCH_DEBOUNCE_MS);
  }

  async applyPitch() {
    const semitones = this.selectedSemitones;
    if (semitones === this.playingSemitones) {
      this.pitchWaiting = false;
      this.renderPitch();
      return;
    }
    if (semitones === 0 || this.job.pitch_versions[semitones] === "done") {
      this.switchKaraoke(semitones);
      return;
    }
    this.pitchWaiting = true;
    this.renderPitch();
    try {
      const response = await fetch(`/api/jobs/${this.job.id}/pitch?semitones=${semitones}`, { method: "POST" });
      if (!response.ok) throw new Error(await readError(response));
      const { status } = await response.json();
      if (semitones !== this.selectedSemitones) return;
      if (status === "done") {
        this.switchKaraoke(semitones);
      } else {
        refreshJobs();
      }
    } catch {
      if (semitones === this.selectedSemitones) this.failPitch();
    }
  }

  /** The server no longer has any version: go back to the original tone, in place. */
  resetPitch() {
    clearTimeout(this.pitchTimer);
    this.pitchTimer = null;
    this.job = { ...this.job, pitch_versions: {} };
    this.selectedSemitones = 0;
    this.pitchWaiting = false;
    this.pitchError = "";
    if (this.playingSemitones !== 0) {
      this.switchKaraoke(0);
    } else {
      this.renderPitch();
    }
  }

  failPitch() {
    this.selectedSemitones = this.playingSemitones;
    this.pitchWaiting = false;
    this.pitchError = "No se pudo ajustar el tono. Inténtalo de nuevo.";
    this.renderPitch();
  }

  karaokeUrl(semitones, download = false) {
    const params = new URLSearchParams();
    if (semitones !== 0) params.set("semitones", semitones);
    if (download) params.set("download", "true");
    const query = params.toString();
    return `/api/jobs/${this.job.id}/karaoke${query ? `?${query}` : ""}`;
  }

  switchKaraoke(semitones) {
    // A swap still loading already holds the real position: currentTime has reset to 0.
    this.pendingSwap ??= { position: this.karaoke.currentTime, wasPlaying: !this.karaoke.paused };
    this.pitchWaiting = false;
    this.playingSemitones = semitones;
    this.karaoke.src = this.karaokeUrl(semitones);
    this.renderPitch();
  }

  finishSwap() {
    const { position, wasPlaying } = this.pendingSwap;
    this.pendingSwap = null;
    this.karaoke.currentTime = position;
    if (wasPlaying) this.karaoke.play().catch(() => this.setPlaying(false));
  }

  renderPitch() {
    const limit = config.max_semitones;
    const settled = this.selectedSemitones === this.playingSemitones;
    this.pitchValue.textContent = formatSemitones(this.selectedSemitones);
    this.pitchDown.disabled = this.selectedSemitones <= -limit;
    this.pitchUp.disabled = this.selectedSemitones >= limit;
    this.toggle.disabled = !settled || this.playingSemitones !== 0;

    if (settled) {
      this.download.href = this.karaokeUrl(this.playingSemitones, true);
      this.download.removeAttribute("aria-disabled");
    } else {
      this.download.removeAttribute("href");
      this.download.setAttribute("aria-disabled", "true");
    }

    let note = "";
    if (this.pitchError) {
      note = this.pitchError;
    } else if (this.pitchWaiting) {
      const estimate = Math.max(
        Math.round(this.job.duration * PITCH_SECONDS_PER_AUDIO_SECOND),
        MIN_PITCH_ESTIMATE_SECONDS,
      );
      note = `Ajustando el tono a ${formatSemitones(this.selectedSemitones)}… unos ${estimate} s. Puedes seguir escuchando.`;
    } else if (settled && this.playingSemitones !== 0) {
      note = "La voz original solo se puede escuchar en el tono original.";
    }
    this.note.textContent = note;
    this.note.hidden = !note;
    this.note.classList.toggle("is-error", Boolean(this.pitchError));
  }

  createAudio(src) {
    const audio = new Audio();
    audio.preload = "metadata";
    audio.src = src;
    audio.addEventListener("timeupdate", () => { if (audio === this.audio) this.syncTimeline(); });
    audio.addEventListener("loadedmetadata", () => {
      if (audio === this.karaoke && this.pendingSwap) this.finishSwap();
      if (audio === this.audio) this.syncTimeline();
    });
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
    return (Number(this.seek.value) / Number(this.seek.max)) * (this.audio.duration || this.job.duration);
  }

  paintSeek() {
    this.seek.style.setProperty("--played", `${(Number(this.seek.value) / Number(this.seek.max)) * 100}%`);
  }

  syncTimeline() {
    if (this.seeking || this.pendingSwap) return;
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
    const wasPlaying = !previous.paused || Boolean(this.pendingSwap?.wasPlaying);
    const position = this.pendingSwap ? this.pendingSwap.position : previous.currentTime;
    if (this.pendingSwap) this.pendingSwap.wasPlaying = false;
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
    clearTimeout(this.pitchTimer);
    this.pitchWaiting = false;
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

function setupConfirmDialog() {
  const { confirmDialog } = elements;
  confirmDialog.addEventListener("close", () => {
    const accepted = confirmDialog.returnValue === "confirm";
    const onAccept = confirmation?.onAccept;
    confirmation = null;
    if (accepted) onAccept?.();
  });
  // A click on the backdrop targets the dialog itself; its content is inside the form.
  confirmDialog.addEventListener("click", (event) => {
    if (event.target === confirmDialog) confirmDialog.close();
  });
}

elements.clearPitch.addEventListener("click", askToClearPitchVersions);
elements.deleteAll.addEventListener("click", askToDeleteAll);

setupDropzone();
setupConfirmDialog();
loadConfig();
refreshJobs();
