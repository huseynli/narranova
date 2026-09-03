const themeToggle = document.querySelector("[data-theme-toggle]");
const themeLabel = document.querySelector("[data-theme-label]");
const colorScheme = window.matchMedia("(prefers-color-scheme: dark)");

function storedTheme() {
  try {
    const localTheme = localStorage.getItem("narranova-theme");
    if (localTheme === "light" || localTheme === "dark") return localTheme;
  } catch (error) {
    // Fall through to the same-site cookie when storage is unavailable.
  }
  const cookieTheme = document.cookie
    .split(";")
    .map((value) => value.trim())
    .find((value) => value.startsWith("narranova_theme="))
    ?.split("=")[1];
  return cookieTheme === "light" || cookieTheme === "dark" ? cookieTheme : null;
}

function applyTheme(theme, persist = false) {
  document.documentElement.dataset.theme = theme;
  if (persist) {
    try {
      localStorage.setItem("narranova-theme", theme);
    } catch (error) {
      // The theme still applies for this page when storage is unavailable.
    }
    document.cookie = `narranova_theme=${theme}; Path=/; Max-Age=31536000; SameSite=Lax`;
  }
  if (!themeToggle || !themeLabel) return;
  const dark = theme === "dark";
  themeToggle.setAttribute("aria-pressed", String(dark));
  themeToggle.setAttribute("aria-label", `Switch to ${dark ? "light" : "dark"} mode`);
  themeToggle.title = `Switch to ${dark ? "light" : "dark"} mode`;
  themeLabel.textContent = dark ? "Light" : "Dark";
}

if (themeToggle) {
  applyTheme(
    document.documentElement.dataset.theme ||
      storedTheme() ||
      (colorScheme.matches ? "dark" : "light")
  );
  themeToggle.addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
  });
  colorScheme.addEventListener("change", (event) => {
    if (!storedTheme()) applyTheme(event.matches ? "dark" : "light");
  });
}

const jobMonitor = document.querySelector("[data-job-monitor]");

function setJobControl(name, visible) {
  const control = document.querySelector(`[data-job-${name}]`);
  if (control) control.hidden = !visible;
}

function addChunkActions(jobId, chunkId, container, regenerationDisabled) {
  if (container.querySelector("audio")) return;
  const base = `/jobs/${encodeURIComponent(jobId)}/chunks/${encodeURIComponent(chunkId)}`;
  const audio = document.createElement("audio");
  audio.controls = true;
  audio.preload = "none";
  audio.src = `${base}/audio`;

  const links = document.createElement("span");
  links.className = "chunk-action-links";
  const regenerateForm = document.createElement("form");
  regenerateForm.method = "post";
  regenerateForm.action = `${base}/regenerate`;
  const csrf = document.createElement("input");
  csrf.type = "hidden";
  csrf.name = "csrf";
  csrf.value = jobMonitor.dataset.csrf;
  const regenerate = document.createElement("button");
  regenerate.className = "chunk-regenerate";
  regenerate.dataset.chunkRegenerate = "";
  regenerate.disabled = regenerationDisabled;
  regenerate.textContent = "Regenerate";
  regenerateForm.append(csrf, regenerate);
  const download = document.createElement("a");
  download.className = "chunk-download";
  download.href = `${base}/download`;
  download.textContent = "Download";
  const remove = document.createElement("a");
  remove.className = "danger-link";
  remove.href = `${base}/delete`;
  remove.textContent = "Delete";
  links.append(regenerateForm, download, remove);
  container.append(audio, links);
}

function formatBytes(size) {
  let value = Number(size);
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (value < 1024 || unit === "GB") {
      return unit === "B" ? `${Math.round(value)} B` : `${value.toFixed(1)} ${unit}`;
    }
    value /= 1024;
  }
  return `${size} B`;
}

function updateOutputArtifacts(state) {
  const container = document.querySelector("[data-output-artifacts]");
  if (!container) return;
  const artifacts = state.artifacts || [];
  const signature = JSON.stringify(artifacts);
  if (container.dataset.signature === signature) return;
  container.dataset.signature = signature;
  container.replaceChildren();
  if (!artifacts.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = state.completed === state.total && state.total > 0
      ? "No deliverables yet. Build the audiobook to create them."
      : "Complete every chunk, then build the audiobook.";
    container.append(empty);
  } else {
    artifacts.forEach((artifact) => {
      const row = document.createElement("article");
      row.className = "output-row";
      const copy = document.createElement("div");
      const title = document.createElement("strong");
      const detail = document.createElement("small");
      if (artifact.kind === "chapter_audio") {
        title.textContent = artifact.title || `Chapter ${artifact.chapter_index}`;
        detail.textContent = `Chapter audio · ${formatBytes(artifact.byte_size)}`;
      } else if (artifact.kind === "audiobook") {
        title.textContent = "Chapterized audiobook";
        detail.textContent = `M4B · ${formatBytes(artifact.byte_size)}`;
      } else {
        title.textContent = "Narration map";
        detail.textContent = `JSON · ${formatBytes(artifact.byte_size)}`;
      }
      copy.append(title, detail);
      const download = document.createElement("a");
      download.className = "button";
      download.href = `/jobs/${encodeURIComponent(jobMonitor.dataset.jobId)}/artifacts/${encodeURIComponent(artifact.id)}/download`;
      download.textContent = "Download";
      row.append(copy, download);
      container.append(row);
    });
  }
  const count = document.querySelector("[data-output-count]");
  if (count) count.textContent = String(artifacts.length).padStart(2, "0");
}

function updateJobPage(state) {
  const status = String(state.status || "ready");
  const statusLabel = document.querySelector("[data-job-status]");
  if (statusLabel) {
    statusLabel.textContent = status;
    statusLabel.className = `status status-${status.replace(/[^a-z_]/g, "")}`;
  }
  const summary = document.querySelector("[data-job-summary]");
  if (summary) summary.textContent = `${state.completed} of ${state.total} chunks complete`;
  const percent = document.querySelector("[data-job-percent]");
  if (percent) percent.textContent = `${state.percent}%`;
  const progress = document.querySelector("[data-job-progress-bar]");
  if (progress) progress.style.width = `${state.percent}%`;

  const error = document.querySelector("[data-job-error]");
  if (error) {
    error.textContent = state.error || "";
    error.hidden = !state.error;
  }

  const jobActive = ["generating", "pause_requested", "assembling"].includes(status);
  const startable = ["ready", "failed", "paused"].includes(status)
    && state.completed < state.total;
  setJobControl("start", startable);
  setJobControl("running", ["generating", "assembling"].includes(status));
  setJobControl("pause", status === "generating" && !state.regenerating);
  setJobControl("pause-requested", status === "pause_requested");
  setJobControl("complete", status === "completed");
  setJobControl("assemble", Boolean(state.can_assemble));
  const runningLabel = document.querySelector("[data-job-running-label]");
  if (runningLabel) {
    runningLabel.textContent = state.assembling || status === "assembling"
      ? "Building audiobook"
      : state.regenerating
      ? "Regenerating chunk"
      : "Generation in progress";
  }
  const assembleLabel = document.querySelector("[data-job-assemble-label]");
  if (assembleLabel) {
    assembleLabel.textContent = state.has_audiobook
      ? "Rebuild audiobook"
      : "Build audiobook";
  }
  updateOutputArtifacts(state);
  const startLabel = document.querySelector("[data-job-start-label]");
  if (startLabel) {
    startLabel.textContent = ["failed", "paused"].includes(status)
      ? "Resume generation"
      : "Start generation";
  }
  document.querySelectorAll("[data-chunk-regenerate]").forEach((button) => {
    button.disabled = jobActive;
  });

  state.chunks.forEach((chunk) => {
    const row = document.querySelector(`[data-chunk-id="${chunk.id}"]`);
    if (!row) return;
    const chunkStatus = row.querySelector("[data-chunk-status]");
    if (chunkStatus) chunkStatus.textContent = chunk.status;
    const metadata = row.querySelector("[data-chunk-meta]");
    if (metadata) {
      const attempts = `${chunk.attempts} attempt${chunk.attempts === 1 ? "" : "s"}`;
      const duration = chunk.duration ? ` · ${Number(chunk.duration).toFixed(1)}s` : "";
      metadata.textContent = attempts + duration;
    }
    if (chunk.status === "completed") {
      const actions = row.querySelector("[data-chunk-actions]");
      if (actions) {
        addChunkActions(jobMonitor.dataset.jobId, chunk.id, actions, jobActive);
      }
    }
  });
  return jobActive;
}

if (jobMonitor) {
  const pollJob = async () => {
    try {
      const response = await fetch(
        `/jobs/${encodeURIComponent(jobMonitor.dataset.jobId)}/status`,
        { cache: "no-store", headers: { Accept: "application/json" } }
      );
      if (!response.ok) throw new Error("Job status request failed");
      const active = updateJobPage(await response.json());
      if (active) window.setTimeout(pollJob, 2000);
    } catch (error) {
      window.setTimeout(pollJob, 4000);
    }
  };
  window.setTimeout(pollJob, 900);
}

document.querySelectorAll("[data-instruction]").forEach((button) => {
  button.addEventListener("click", () => {
    const field = document.querySelector("#instruction");
    if (!field) return;
    field.value = button.dataset.instruction || "";
    field.focus();
    document.querySelectorAll("[data-instruction]").forEach((item) => {
      item.classList.toggle("selected", item === button);
    });
  });
});

const providerSelect = document.querySelector("[data-provider-select]");
const profileSelect = document.querySelector("[data-profile-select]");

function filterProfiles() {
  if (!providerSelect || !profileSelect) return;
  const providerId = providerSelect.value;
  const providerKind = providerSelect.selectedOptions[0]?.dataset.kind || "";
  let firstVisible = null;
  Array.from(profileSelect.options).forEach((option) => {
    const visible =
      option.dataset.provider === providerId ||
      (!option.dataset.provider && option.dataset.kind === providerKind);
    option.hidden = !visible;
    option.disabled = !visible;
    if (visible && !firstVisible) firstVisible = option;
  });
  if (!profileSelect.selectedOptions[0] || profileSelect.selectedOptions[0].disabled) {
    profileSelect.value = firstVisible ? firstVisible.value : "";
  }
  document.querySelectorAll("[data-default-kind]").forEach((card) => {
    card.hidden = card.dataset.defaultKind !== providerKind;
  });
  document.querySelectorAll("[data-custom-provider]").forEach((card) => {
    card.hidden = card.dataset.customProvider !== providerId;
  });
  document.querySelectorAll("[data-custom-preview]").forEach((preview) => {
    const hasVisiblePair = Array.from(
      preview.querySelectorAll("[data-custom-provider]")
    ).some((card) => !card.hidden);
    preview.hidden = !hasVisiblePair;
  });
}

if (providerSelect && profileSelect) {
  providerSelect.addEventListener("change", filterProfiles);
  filterProfiles();
}

const studioProvider = document.querySelector("[data-studio-provider]");

function updateStudioModules() {
  if (!studioProvider) return;
  const option = studioProvider.selectedOptions[0];
  const instructions = option?.dataset.instructions !== "false";
  const reference = option?.dataset.reference !== "false";
  document.querySelectorAll('[data-studio-module="instructions"]').forEach((module) => {
    module.hidden = !instructions;
    module.querySelectorAll("textarea, input, select").forEach((field) => {
      field.disabled = !instructions;
    });
  });
  document.querySelectorAll('[data-studio-module="reference"]').forEach((module) => {
    module.hidden = !reference;
    module.querySelectorAll("textarea, input, select").forEach((field) => {
      field.disabled = !reference;
    });
  });
}

if (studioProvider) {
  studioProvider.addEventListener("change", updateStudioModules);
  updateStudioModules();
}
