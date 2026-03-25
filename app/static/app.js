const appState = {
  snapshot: null,
  destinationDir: localStorage.getItem("sd_dashboard_destination") || "",
  cameraName: "backup_image_1",
  selectedSource: "",
  selectedImage: "",
  selectedTargets: {},
  targetImages: {},
  wipeBefore: {},
  expandedLogs: loadExpandedLogs(),
  frozenLogs: loadFrozenLogs(),
  logScrollTop: {},
  refreshInFlight: false,
};
const SYSTEM_LOG_KEY = "__system__";
const MULTI_CARD_FLASH_BASELINE_SECONDS = 12.5 * 60;

document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  document.getElementById("camera-name").value = appState.cameraName;
  if (appState.destinationDir) {
    document.getElementById("destination-dir").value = appState.destinationDir;
    document.getElementById("backup-destination-dir").value = appState.destinationDir;
  }
  refreshState();
  window.setInterval(refreshState, 4000);
});

function bindEvents() {
  const manualImagePathInput = document.getElementById("manual-image-path");
  const imageList = document.getElementById("image-list");

  document.getElementById("refresh-button").addEventListener("click", refreshState);

  document.getElementById("destination-dir").addEventListener("input", (event) => {
    appState.destinationDir = event.target.value.trim();
  });

  document.getElementById("destination-dir").addEventListener("change", (event) => {
    appState.destinationDir = event.target.value.trim();
    localStorage.setItem("sd_dashboard_destination", appState.destinationDir);
    refreshState();
  });

  document.getElementById("backup-destination-dir").addEventListener("input", (event) => {
    appState.destinationDir = event.target.value.trim();
  });

  document.getElementById("backup-destination-dir").addEventListener("change", (event) => {
    appState.destinationDir = event.target.value.trim();
    localStorage.setItem("sd_dashboard_destination", appState.destinationDir);
    refreshState();
  });

  document.getElementById("camera-name").addEventListener("input", (event) => {
    appState.cameraName = event.target.value;
  });

  document.getElementById("source-device").addEventListener("change", (event) => {
    appState.selectedSource = event.target.value;
  });

  if (manualImagePathInput) {
    manualImagePathInput.addEventListener("input", (event) => {
      appState.selectedImage = event.target.value.trim();
      renderDevices();
      renderFlashSummary();
      renderImages();
    });
  }

  document.getElementById("backup-form").addEventListener("submit", async (event) => {
    event.preventDefault();

    if (!appState.selectedSource) {
      window.alert("Select a source SD card first.");
      return;
    }

    if (!appState.cameraName.trim()) {
      window.alert("Enter a backup image name before creating the backup image.");
      return;
    }

    try {
      await postJson("/api/golden", {
        source_device: appState.selectedSource,
        camera_name: appState.cameraName.trim(),
        destination_dir: currentDestination(),
      });
      refreshState();
    } catch (error) {
      window.alert(error.message);
    }
  });

  document.getElementById("flash-form").addEventListener("submit", async (event) => {
    event.preventDefault();

    const targets = Object.entries(appState.selectedTargets)
      .filter(([, selected]) => selected)
      .map(([devicePath]) => ({
        device_path: devicePath,
        wipe_first: Boolean(appState.wipeBefore[devicePath]),
        image_path: selectedImagePathForDevice(devicePath),
      }));

    if (targets.length === 0) {
      window.alert("Select at least one target SD card.");
      return;
    }

    if (targets.some((target) => !target.image_path)) {
      window.alert("Choose a backup image for every selected target card.");
      return;
    }

    try {
      await postJson("/api/flash", {
        targets,
        destination_dir: currentDestination(),
      });
      refreshState();
    } catch (error) {
      window.alert(error.message);
    }
  });

  if (imageList) {
    imageList.addEventListener("change", (event) => {
      const radio = event.target.closest("input[type='radio'][name='selected-image']");
      if (!radio) {
        return;
      }
      appState.selectedImage = radio.value;
      if (manualImagePathInput) {
        setInputValueIfIdle(manualImagePathInput, radio.value);
      }
      renderDevices();
      renderImages();
      renderFlashSummary();
    });
  }

  document.getElementById("device-grid").addEventListener("change", (event) => {
    const imageSelect = event.target.closest("[data-image-device]");
    if (imageSelect) {
      appState.targetImages[imageSelect.dataset.imageDevice] = imageSelect.value;
      renderFlashSummary();
      return;
    }

    const targetToggle = event.target.closest("[data-target-device]");
    if (targetToggle) {
      appState.selectedTargets[targetToggle.dataset.targetDevice] = targetToggle.checked;
      renderFlashSummary();
      return;
    }

    const wipeToggle = event.target.closest("[data-wipe-device]");
    if (wipeToggle) {
      appState.wipeBefore[wipeToggle.dataset.wipeDevice] = wipeToggle.checked;
    }
  });

  document.getElementById("device-grid").addEventListener("click", async (event) => {
    const wipeButton = event.target.closest("[data-action='wipe']");
    if (wipeButton) {
      const device = wipeButton.dataset.device;
      const confirmed = window.confirm(
        `Delete partitions on ${device} and reformat it as FAT32?`
      );
      if (!confirmed) {
        return;
      }

      try {
        await postJson("/api/wipe", {
          device,
          destination_dir: currentDestination(),
        });
        refreshState();
      } catch (error) {
        window.alert(error.message);
      }
      return;
    }

    const ejectButton = event.target.closest("[data-action='eject']");
    if (ejectButton) {
      const device = ejectButton.dataset.device;
      const confirmed = window.confirm(`Eject ${device}?`);
      if (!confirmed) {
        return;
      }

      try {
        await postJson("/api/eject", {
          device,
          destination_dir: currentDestination(),
        });
        refreshState();
      } catch (error) {
        window.alert(error.message);
      }
    }
  });

  document.getElementById("eject-all-button").addEventListener("click", async () => {
    const confirmed = window.confirm("Eject all removable SD cards shown in the dashboard?");
    if (!confirmed) {
      return;
    }

    try {
      await postJson("/api/eject-all", {
        destination_dir: currentDestination(),
      });
      refreshState();
    } catch (error) {
      window.alert(error.message);
    }
  });

  document.getElementById("clear-all-logs-button").addEventListener("click", async () => {
    const confirmed = window.confirm("Clear the System Log from Live Activity?");
    if (!confirmed) {
      return;
    }

    try {
      await postJson("/api/logs/clear", {});
      refreshState();
    } catch (error) {
      window.alert(error.message);
    }
  });

  document.getElementById("show-system-log-button").addEventListener("click", () => {
    appState.expandedLogs[SYSTEM_LOG_KEY] = true;
    persistExpandedLogs();
    renderJobs();
  });

  document.getElementById("toggle-system-log-button").addEventListener("click", () => {
    appState.expandedLogs[SYSTEM_LOG_KEY] = !systemLogVisible();
    persistExpandedLogs();
    renderJobs();
  });

  document.getElementById("freeze-system-log-button").addEventListener("click", () => {
    appState.frozenLogs[SYSTEM_LOG_KEY] = !systemLogFrozen();
    persistFrozenLogs();
    renderJobs();
  });

  document.getElementById("copy-system-log-button").addEventListener("click", async () => {
    const text = systemLogText();
    if (!text) {
      return;
    }
    try {
      await copyTextToClipboard(text);
    } catch (error) {
      window.alert(`Could not copy log: ${error.message}`);
    }
  });

  document.getElementById("job-list").addEventListener("click", async (event) => {
    const deleteButton = event.target.closest("[data-action='delete-job']");
    if (deleteButton) {
      const jobId = deleteButton.dataset.job;
      try {
        await postJson("/api/jobs/delete", { job_id: jobId });
        refreshState();
      } catch (error) {
        window.alert(error.message);
      }
      return;
    }
  });

  document.getElementById("system-log-body").addEventListener(
    "scroll",
    (event) => {
      const logBlock = event.target.closest(".log-block[data-job-log]");
      if (!logBlock) {
        return;
      }
      appState.logScrollTop[SYSTEM_LOG_KEY] = logBlock.scrollTop;
    },
    true
  );
}

async function refreshState() {
  if (appState.refreshInFlight) {
    return;
  }

  appState.refreshInFlight = true;
  try {
    const params = new URLSearchParams();
    if (currentDestination()) {
      params.set("destination_dir", currentDestination());
    }
    const response = await fetch(`/api/state?${params.toString()}`, { cache: "no-store" });
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Failed to refresh dashboard state.");
    }

    appState.snapshot = payload;
    if (!appState.destinationDir) {
      appState.destinationDir =
        payload.system.destination_dir || payload.system.default_destination_dir;
      localStorage.setItem("sd_dashboard_destination", appState.destinationDir);
    }

    syncSelections();
    renderAll();
  } catch (error) {
    console.error(error);
    renderWarnings([`Dashboard refresh failed: ${error.message}`]);
  } finally {
    appState.refreshInFlight = false;
  }
}

function syncSelections() {
  if (!appState.snapshot) {
    return;
  }

  const usableDevices = appState.snapshot.devices.filter((device) => device.is_candidate);
  const availableSources = usableDevices.filter((device) => !device.busy_job_id);

  if (!availableSources.some((device) => device.path === appState.selectedSource)) {
    const preferredSource =
      availableSources.find((device) => device.recommended_source) || availableSources[0];
    appState.selectedSource = preferredSource ? preferredSource.path : "";
  }

  const allowedTargets = new Set(
    usableDevices.filter((device) => !device.busy_job_id).map((device) => device.path)
  );
  Object.keys(appState.selectedTargets).forEach((devicePath) => {
    if (!allowedTargets.has(devicePath)) {
      delete appState.selectedTargets[devicePath];
      delete appState.wipeBefore[devicePath];
    }
  });

  const knownTargetDevices = new Set(usableDevices.map((device) => device.path));
  Object.keys(appState.targetImages).forEach((devicePath) => {
    if (!knownTargetDevices.has(devicePath)) {
      delete appState.targetImages[devicePath];
    }
  });

  const knownImages = new Set(appState.snapshot.images.map((image) => image.path));
  if (!appState.selectedImage) {
    appState.selectedImage = appState.snapshot.images[0]?.path || "";
  } else if (knownImages.size && !knownImages.has(appState.selectedImage)) {
    const manualInput = document.getElementById("manual-image-path");
    if (document.activeElement !== manualInput) {
      appState.selectedImage = appState.snapshot.images[0]?.path || appState.selectedImage;
    }
  }

  const validJobIds = new Set(appState.snapshot.jobs.map((job) => job.id));
  let dirty = false;
  Object.keys(appState.expandedLogs).forEach((jobId) => {
    if (jobId !== SYSTEM_LOG_KEY && !validJobIds.has(jobId)) {
      delete appState.expandedLogs[jobId];
      dirty = true;
    }
  });
  Object.keys(appState.frozenLogs).forEach((jobId) => {
    if (jobId !== SYSTEM_LOG_KEY && !validJobIds.has(jobId)) {
      delete appState.frozenLogs[jobId];
      delete appState.logScrollTop[jobId];
      dirty = true;
    }
  });
  if (dirty) {
    persistExpandedLogs();
    persistFrozenLogs();
  }
}

function renderAll() {
  renderSystem();
  renderSourceOptions();
  renderImages();
  renderDevices();
  renderFlashSummary();
  renderJobs();

  setInputValueIfIdle(document.getElementById("destination-dir"), currentDestination());
  setInputValueIfIdle(
    document.getElementById("backup-destination-dir"),
    currentDestination()
  );
  const manualImagePathInput = document.getElementById("manual-image-path");
  if (manualImagePathInput) {
    setInputValueIfIdle(manualImagePathInput, appState.selectedImage || "");
  }
}

function renderSystem() {
  const snapshot = appState.snapshot;
  if (!snapshot) {
    return;
  }

  const cpuPercent = formatPercentValue(snapshot.system.cpu_usage_percent);
  const memoryUsed = snapshot.system.memory?.used_bytes;
  const memoryTotal = snapshot.system.memory?.total_bytes;
  const memoryUsedLabel = formatBytes(memoryUsed);
  const memoryMeta = formatMemoryMeta(snapshot.system.memory?.used_percent, memoryTotal);
  const diskIoLabel = formatRate(snapshot.system.disk_io_bps);

  document.getElementById("system-stats").innerHTML = `
    <div class="system-stat-card cpu">
      <div class="system-stat-copy">
        <div class="system-stat-label">CPU Performance</div>
        <div class="system-stat-value">${escapeHtml(cpuPercent)}</div>
        <div class="system-stat-meta">Current system utilization</div>
      </div>
      <div class="system-stat-orb cpu" aria-hidden="true">
        <div class="system-stat-orb-core">CPU</div>
      </div>
    </div>
    <div class="system-stat-card memory">
      <div class="system-stat-copy">
        <div class="system-stat-label">Memory Usage</div>
        <div class="system-stat-value">${escapeHtml(memoryUsedLabel)}</div>
        <div class="system-stat-meta">${escapeHtml(memoryMeta)}</div>
      </div>
      <div class="system-stat-orb memory" aria-hidden="true">
        <div class="system-stat-orb-core">RAM</div>
      </div>
    </div>
    <div class="system-stat-card disk">
      <div class="system-stat-copy">
        <div class="system-stat-label">Disk I/O</div>
        <div class="system-stat-value">${escapeHtml(diskIoLabel)}</div>
        <div class="system-stat-meta">Combined read and write throughput</div>
      </div>
      <div class="system-stat-orb disk" aria-hidden="true">
        <div class="system-stat-orb-core">IO</div>
      </div>
    </div>
  `;
  renderWarnings(snapshot.system.warnings || []);
}

function renderWarnings(warnings) {
  const warningList = document.getElementById("warning-list");
  if (!warnings || warnings.length === 0) {
    warningList.innerHTML = "";
    return;
  }
  warningList.innerHTML = warnings
    .map((warning) => `<div class="warning">${escapeHtml(warning)}</div>`)
    .join("");
}

function renderSourceOptions() {
  const select = document.getElementById("source-device");
  const snapshot = appState.snapshot;
  if (!snapshot) {
    return;
  }

  const options = snapshot.devices
    .filter((device) => device.is_candidate)
    .map((device) => {
      return `<option value="${escapeHtml(device.path)}" ${
        device.path === appState.selectedSource ? "selected" : ""
      } ${device.busy_job_id ? "disabled" : ""}>${escapeHtml(`${device.path} | ${device.size_human}`)}</option>`;
    });

  if (options.length === 0) {
    select.innerHTML = `<option value="">No removable SD card found</option>`;
    select.disabled = true;
    return;
  }

  select.innerHTML = options.join("");
  select.disabled = false;
}

function renderImages() {
  const container = document.getElementById("image-list");
  if (!container) {
    return;
  }
  const snapshot = appState.snapshot;
  if (!snapshot) {
    return;
  }

  if (!snapshot.images.length) {
    container.classList.add("empty-state");
    container.innerHTML = "No .img backup files found in the selected destination directory.";
    return;
  }

  container.classList.remove("empty-state");
  container.innerHTML = snapshot.images
    .map((image) => {
      const selected = image.path === appState.selectedImage;
      return `
        <label class="image-card ${selected ? "selected" : ""}">
          <div class="image-radio">
            <input
              type="radio"
              name="selected-image"
              value="${escapeHtml(image.path)}"
              ${selected ? "checked" : ""}
            />
            <div>
              <div class="image-top">
                <div>
                  <div class="image-name">${escapeHtml(image.name)}</div>
                  <div class="image-path">${escapeHtml(image.path)}</div>
                </div>
                ${badge(image.size_human, "info")}
              </div>
              <div class="image-meta">
                <div>Modified: ${formatDateTime(image.modified_at)}</div>
              </div>
            </div>
          </div>
        </label>
      `;
    })
    .join("");
}

function renderDevices() {
  const container = document.getElementById("device-grid");
  const snapshot = appState.snapshot;
  if (!snapshot) {
    return;
  }

  const ejectableDevices = snapshot.devices.filter(
    (device) => device.is_candidate && !device.busy_job_id
  );
  document.getElementById("eject-all-button").disabled = ejectableDevices.length === 0;

  if (!snapshot.devices.length) {
    container.innerHTML =
      `<div class="empty-state card-placeholder">No removable devices detected.</div>`;
    return;
  }

  container.innerHTML = snapshot.devices
    .map((device, index) => {
      const busy = Boolean(device.busy_job_id);
      const canSelect = device.is_candidate && !busy;
      const checked = Boolean(appState.selectedTargets[device.path]);
      const wipeChecked = Boolean(appState.wipeBefore[device.path]);
      const cardNumber = device.display_index;
      const hasMedia = Number(device.size_bytes || 0) > 0;
      const selectedTargetImage = selectedImagePathForDevice(device.path);
      const imageOptions = renderDeviceImageOptions(selectedTargetImage);
      const mountpoints = device.mountpoints.length
        ? device.mountpoints
            .map(
              (mountpoint) => `<div class="mountpoint-line">${escapeHtml(mountpoint)}</div>`
            )
            .join("")
        : `<div class="mountpoint-line placeholder">No active mountpoints</div>`;
      const partitions = device.partitions.length
        ? device.partitions
            .map(
              (partition) => `
                <div class="partition-line">
                  <strong>${escapeHtml(partition.path)}</strong>
                  ${partition.label ? ` | ${escapeHtml(partition.label)}` : ""}
                  ${partition.fstype ? ` | ${escapeHtml(partition.fstype)}` : ""}
                  ${
                    partition.mountpoints.length
                      ? ` | ${escapeHtml(partition.mountpoints.join(", "))}`
                      : ""
                  }
                </div>
              `
            )
            .join("")
        : `<div class="partition-line placeholder">No partitions detected</div>`;

      const readinessMessage = !hasMedia
        ? "No SD card detected in this reader."
        : device.is_protected
          ? `Protected: ${device.protected_reason || "This drive cannot be changed here."}`
          : "Ready for backup or write";

      const helperMessage = !hasMedia
        ? "Insert an SD card to enable backup and write actions."
        : device.is_protected
          ? (device.protected_reason || "This drive is protected from destructive actions.")
          : "Choose an action below.";

      const statusBadges = [
        device.capacity_bucket ? badge(device.capacity_bucket, "info") : "",
        !hasMedia ? badge("No SD Card Detected", "warn") : "",
        device.is_protected ? badge("Protected", "danger") : "",
        busy ? badge(`Busy (${device.busy_job_id})`, "warn") : "",
      ]
        .filter(Boolean)
        .join("");

      return `
        <article class="device-card ${device.is_protected ? "protected" : ""} ${!hasMedia ? "empty-reader" : ""}">
          <div class="device-top">
            <div class="device-title-stack">
              <div class="device-heading">
                ${
                  cardNumber
                    ? `<span class="device-order-badge">${escapeHtml(String(cardNumber))}</span>`
                    : ""
                }
                <div class="device-name">${escapeHtml(device.path)}</div>
              </div>
              <div class="device-status-line ${!hasMedia ? "warn" : device.is_protected ? "danger" : "ok"}">
                ${escapeHtml(readinessMessage)}
              </div>
            </div>
            <div class="badge-row">${statusBadges}</div>
          </div>

          <div class="device-meta">
            <div>Size: ${escapeHtml(device.size_human)}</div>
            <div>${escapeHtml(helperMessage)}</div>
          </div>

          <div class="mountpoints">
            <strong>Mountpoints</strong>
            ${mountpoints}
          </div>

          <div class="partition-block">
            <strong>Partitions</strong>
            ${partitions}
          </div>

          <div class="device-actions">
            ${
              device.is_candidate
                ? `
                  <div class="device-image-picker">
                    <label class="device-field-label">Image to Write</label>
                    <select data-image-device="${escapeHtml(device.path)}" ${
                      busy || !snapshot.images.length ? "disabled" : ""
                    }>
                      ${imageOptions}
                    </select>
                  </div>
                `
                : ""
            }
            ${
              canSelect
                ? `
                  <label class="checkbox-line">
                    <input type="checkbox" data-target-device="${escapeHtml(device.path)}" ${checked ? "checked" : ""} />
                    Select this card as a write target
                  </label>
                  <label class="checkbox-line">
                    <input type="checkbox" data-wipe-device="${escapeHtml(device.path)}" ${wipeChecked ? "checked" : ""} />
                    Delete partitions before writing
                  </label>
                `
                : ""
            }
            <div class="action-row">
              ${
                device.is_candidate && device.has_partitions && !busy
                  ? `<button class="button device-action-button" type="button" data-action="wipe" data-device="${escapeHtml(device.path)}">Delete and Reformat to FAT32</button>`
                  : ""
              }
              ${
                device.is_candidate && !busy
                  ? `<button class="button device-action-button" type="button" data-action="eject" data-device="${escapeHtml(device.path)}">Eject Card</button>`
                  : ""
              }
            </div>
          </div>
        </article>
      `;
    })
    .join("");
}

function defaultImagePath() {
  const images = appState.snapshot?.images || [];
  if (!images.length) {
    return "";
  }

  const knownImages = new Set(images.map((image) => image.path));
  if (appState.selectedImage && knownImages.has(appState.selectedImage)) {
    return appState.selectedImage;
  }
  return images[0].path;
}

function renderDeviceImageOptions(selectedPath) {
  const images = appState.snapshot?.images || [];
  if (!images.length) {
    return `<option value="">No backup images found</option>`;
  }

  return images
    .map((image) => {
      const label = `${image.name} | ${image.size_human}`;
      return `<option value="${escapeHtml(image.path)}" ${
        image.path === selectedPath ? "selected" : ""
      }>${escapeHtml(label)}</option>`;
    })
    .join("");
}

function selectedImagePathForDevice(devicePath) {
  const images = appState.snapshot?.images || [];
  const knownImages = new Set(images.map((image) => image.path));
  const explicitSelection = appState.targetImages[devicePath];
  if (explicitSelection && knownImages.has(explicitSelection)) {
    return explicitSelection;
  }
  return defaultImagePath();
}

function selectedFlashPlans() {
  return Object.entries(appState.selectedTargets)
    .filter(([, selected]) => selected)
    .map(([devicePath]) => {
      const imagePath = selectedImagePathForDevice(devicePath);
      return {
        devicePath,
        imagePath,
        imageName: imagePath ? fileName(imagePath) : "",
      };
    });
}

function renderFlashSummary() {
  const plans = selectedFlashPlans();
  const selectedTargets = plans.length;
  const uniqueImageNames = [...new Set(plans.map((plan) => plan.imageName).filter(Boolean))];

  document.getElementById("selected-target-count").textContent = `${selectedTargets} ${
    selectedTargets === 1 ? "card" : "cards"
  }`;
  document.getElementById("selected-image-name").textContent =
    uniqueImageNames.length === 0
      ? "None selected"
      : uniqueImageNames.length === 1
        ? uniqueImageNames[0]
        : `${uniqueImageNames.length} backups selected`;

  const flashPlanList = document.getElementById("flash-plan-list");
  if (!plans.length) {
    flashPlanList.innerHTML =
      `<div class="flash-plan-empty">Select cards to see which backup will write to each target.</div>`;
  } else {
    flashPlanList.innerHTML = plans
      .map(
        (plan) => `
          <div class="flash-plan-item">
            <div class="flash-plan-device">${escapeHtml(plan.devicePath)}</div>
            <div class="flash-plan-arrow">writes</div>
            <div class="flash-plan-image">${escapeHtml(plan.imageName || "No image selected")}</div>
          </div>
        `
      )
      .join("");
  }

  const flashButton = document.getElementById("flash-submit");
  const ready =
    selectedTargets > 0 &&
    plans.every((plan) => Boolean(plan.imagePath)) &&
    appState.snapshot?.system.running_as_root;
  flashButton.disabled = !ready;
  flashButton.textContent = uniqueImageNames.length > 1 ? "Write Selected Backups" : "Write Selected Backup";
}

function renderJobs() {
  const container = document.getElementById("job-list");
  const snapshot = appState.snapshot;
  if (!snapshot) {
    return;
  }

  const liveActivityShell = document.getElementById("live-activity-shell");
  const systemLogPanel = document.getElementById("system-log-panel");
  const showSystemLogButton = document.getElementById("show-system-log-button");
  const freezeSystemLogButton = document.getElementById("freeze-system-log-button");
  const copySystemLogButton = document.getElementById("copy-system-log-button");
  const toggleSystemLogButton = document.getElementById("toggle-system-log-button");
  const clearAllButton = document.getElementById("clear-all-logs-button");
  const aggregatedEntries = systemLogEntries(snapshot.jobs);
  const logVisible = systemLogVisible();
  const logFrozen = systemLogFrozen();

  liveActivityShell.classList.toggle("log-hidden", !logVisible);
  systemLogPanel.hidden = !logVisible;
  showSystemLogButton.hidden = logVisible;
  freezeSystemLogButton.textContent = logFrozen ? "Unfreeze Log" : "Freeze Log";
  toggleSystemLogButton.textContent = logVisible ? "Hide System Log" : "Show System Log";
  clearAllButton.disabled = aggregatedEntries.length === 0;
  copySystemLogButton.disabled = aggregatedEntries.length === 0;
  freezeSystemLogButton.disabled = aggregatedEntries.length === 0;

  if (!snapshot.jobs.length) {
    container.innerHTML = `<div class="empty-state card-placeholder">No jobs started yet.</div>`;
  } else {
    container.innerHTML = snapshot.jobs
      .map((job) => {
        const tone = statusTone(job.status);
        const percentValue = overallJobProgressPercent(job, snapshot.now);
        const percent = percentValue.toFixed(1);
        const elapsed = formatElapsed(job, snapshot.now);
        const summary = summarizeJob(job, snapshot.now, percentValue);
        const targetCards = renderActivityTargets(job.targets || []);

        return `
          <article class="job-card activity-card tone-${tone}">
            <button
              class="tile-delete"
              type="button"
              data-action="delete-job"
              data-job="${escapeHtml(job.id)}"
              aria-label="Delete activity tile"
              title="${job.status === "running" || job.status === "queued" ? "Cannot delete while running" : "Delete activity tile"}"
              ${job.status === "running" || job.status === "queued" ? "disabled" : ""}
            >
              ×
            </button>

            <div class="activity-card-header">
              <div class="activity-icon tone-${tone}">
                <span class="activity-icon-core">${escapeHtml(activityIconLabel(job))}</span>
              </div>
              <div class="activity-heading">
                <div class="job-title">${escapeHtml(job.title)}</div>
                <div class="activity-subtitle">${escapeHtml(formatJobSubtitle(job))}</div>
              </div>
              <div class="activity-status-pill tone-${tone}">${escapeHtml(formatStatusLabel(job.status))}</div>
            </div>

            <div class="activity-metric-grid">
              <div class="activity-metric-card">
                <div class="activity-metric-label">Current Speed</div>
                <div class="activity-metric-value">${escapeHtml(summary.speedLabel)}</div>
              </div>
              <div class="activity-metric-card">
                <div class="activity-metric-label">Time Remaining</div>
                <div class="activity-metric-value">${escapeHtml(summary.remainingLabel)}</div>
              </div>
              <div class="activity-metric-card">
                <div class="activity-metric-label">Processed</div>
                <div class="activity-metric-value processed">${escapeHtml(summary.processedLabel)}</div>
              </div>
            </div>

            <div class="activity-elapsed-row">
              <span>Elapsed Time</span>
              <strong>${escapeHtml(elapsed)}</strong>
            </div>

            <div class="activity-progress-header">
              <span>${escapeHtml(activityProgressLabel(job))}</span>
              <strong>${percent}%</strong>
            </div>
            <div class="progress activity-progress tone-${tone}"><span style="width:${percent}%"></span></div>

            <div class="activity-message-row">
              <span>${escapeHtml(job.phase || "Activity")}</span>
              <span>${escapeHtml(job.message || "")}</span>
            </div>

            ${targetCards ? `<div class="activity-target-list">${targetCards}</div>` : ""}

            <div class="activity-card-footer">
              <div class="activity-time-meta">
                Created: ${formatDateTime(job.created_at)}
                ${job.finished_at ? ` | Finished: ${formatDateTime(job.finished_at)}` : ""}
              </div>
            </div>
          </article>
        `;
      })
      .join("");
  }

  renderSystemLog(aggregatedEntries);
  syncSystemLogBlock();
}

function renderSystemLog(entries) {
  const logVisible = systemLogVisible();
  const logFrozen = systemLogFrozen();
  const logBody = document.getElementById("system-log-body");

  if (!logVisible) {
    logBody.innerHTML = "";
    return;
  }

  if (!entries.length) {
    logBody.innerHTML = `
      <div class="log-empty system-log-empty">
        <span class="system-log-empty-caret">&gt;</span>
        Listening for system activity...
      </div>
    `;
    return;
  }

  logBody.innerHTML = `
    <div class="log-block system-log-block ${logFrozen ? "frozen" : "live"}" data-job-log="${SYSTEM_LOG_KEY}">
      ${entries.map((entry) => renderSystemLogEntry(entry)).join("")}
    </div>
  `;
}

function renderSystemLogEntry(entry) {
  const tone = logEntryTone(entry.line);
  return `
    <div class="system-log-entry ${tone}">
      <span class="system-log-time">[${escapeHtml(formatLogTime(entry.ts))}]</span>
      <span class="system-log-line">${escapeHtml(entry.line)}</span>
    </div>
  `;
}

function systemLogEntries(jobs) {
  return (jobs || [])
    .flatMap((job) =>
      (job.logs || []).map((entry, index) => ({
        ts: entry.ts,
        line: entry.line,
        order: index,
        jobId: job.id,
      }))
    )
    .sort((a, b) => {
      if (a.ts === b.ts) {
        return a.order - b.order;
      }
      return a.ts - b.ts;
    });
}

function systemLogText() {
  return systemLogEntries(appState.snapshot?.jobs || [])
    .map((entry) => `[${formatLogTime(entry.ts)}] ${entry.line}`)
    .join("\n");
}

function systemLogVisible() {
  return appState.expandedLogs[SYSTEM_LOG_KEY] !== false;
}

function systemLogFrozen() {
  return Boolean(appState.frozenLogs[SYSTEM_LOG_KEY]);
}

function logEntryTone(line) {
  const text = (line || "").toLowerCase();
  if (text.includes("error") || text.includes("failed")) {
    return "danger";
  }
  if (text.includes("warning")) {
    return "warn";
  }
  if (text.includes("success") || text.includes("completed")) {
    return "ok";
  }
  return "info";
}

function summarizeJob(job, nowTimestamp, percentValue) {
  if (usesTimedMultiCardFlashModel(job)) {
    const targetSummary = summarizeTargetMetrics(job.targets || []);
    return {
      speedLabel: targetSummary.speedBps ? formatRate(targetSummary.speedBps) : fallbackSpeedLabel(job),
      remainingLabel: formatTimedMultiCardFlashRemaining(job, nowTimestamp),
      processedLabel: formatProcessedLabel(
        targetSummary.processedBytes,
        targetSummary.totalBytes,
        percentValue
      ),
    };
  }

  const targetSummary = summarizeTargetMetrics(job.targets || []);
  const summary = targetSummary.totalBytes > 0 ? targetSummary : summarizeStandaloneMetrics(job);
  return {
    speedLabel: summary.speedBps ? formatRate(summary.speedBps) : fallbackSpeedLabel(job),
    remainingLabel: formatRemainingLabel(summary.etaSeconds, job.status),
    processedLabel: formatProcessedLabel(summary.processedBytes, summary.totalBytes, percentValue),
  };
}

function summarizeTargetMetrics(targets) {
  return targets.reduce(
    (summary, target) => {
      const totalBytes = Number(target.metrics?.total_bytes ?? target.image_size ?? 0);
      const processedBytes =
        target.metrics?.processed_bytes != null
          ? Number(target.metrics.processed_bytes)
          : target.status === "completed"
            ? totalBytes
            : 0;
      const speedBps = Number(target.metrics?.speed_bps ?? 0);
      const etaSeconds = Number(target.metrics?.eta_seconds ?? 0);
      summary.totalBytes += totalBytes;
      summary.processedBytes += processedBytes;
      summary.speedBps += speedBps;
      if (etaSeconds > 0) {
        summary.etaSeconds = summary.etaSeconds > 0 ? Math.max(summary.etaSeconds, etaSeconds) : etaSeconds;
      }
      return summary;
    },
    { totalBytes: 0, processedBytes: 0, speedBps: 0, etaSeconds: null }
  );
}

function summarizeStandaloneMetrics(job) {
  return {
    totalBytes: Number(job.metrics?.total_bytes ?? 0),
    processedBytes: Number(job.metrics?.processed_bytes ?? 0),
    speedBps: Number(job.metrics?.speed_bps ?? 0),
    etaSeconds:
      job.metrics?.eta_seconds != null ? Number(job.metrics.eta_seconds) : null,
  };
}

function fallbackSpeedLabel(job) {
  if (job.status === "completed") {
    return "Done";
  }
  if (job.status === "failed") {
    return "Stopped";
  }
  return "Live";
}

function formatRemainingLabel(etaSeconds, status) {
  if (etaSeconds != null && Number.isFinite(Number(etaSeconds)) && Number(etaSeconds) > 0) {
    return formatDuration(etaSeconds);
  }
  if (status === "completed") {
    return "Done";
  }
  if (status === "failed") {
    return "Stopped";
  }
  return "Tracking";
}

function formatProcessedLabel(processedBytes, totalBytes, percent) {
  if (totalBytes && processedBytes >= 0) {
    return `${formatBytes(processedBytes)} / ${formatBytes(totalBytes)}`;
  }
  return `${clampPercent(percent).toFixed(1)}%`;
}

function renderActivityTargets(targets) {
  if (!targets.length) {
    return "";
  }

  return targets
    .map((target) => {
      const tone = statusTone(target.status || "queued");
      const percent = progressPercent(target).toFixed(1);
      const metrics = formatMetrics(target.metrics);
      return `
        <div class="activity-target-card tone-${tone}">
          <div class="activity-target-top">
            <div class="activity-target-device">${escapeHtml(target.device_path)}</div>
            <div class="activity-target-status tone-${tone}">${escapeHtml(formatStatusLabel(target.status || "queued"))}</div>
          </div>
          ${
            target.image_name
              ? `<div class="activity-target-meta">${escapeHtml(target.image_name)}</div>`
              : ""
          }
          <div class="activity-target-meta">${escapeHtml(target.message || target.phase || "")}</div>
          <div class="activity-target-progress">
            <span>${percent}%</span>
            <span>${escapeHtml(metrics || "Pending")}</span>
          </div>
        </div>
      `;
    })
    .join("");
}

function activityIconLabel(job) {
  const jobType = (job.type || "activity").toUpperCase();
  if (jobType === "FLASH") {
    return "WR";
  }
  if (jobType === "GOLDEN") {
    return "BK";
  }
  if (jobType === "WIPE") {
    return "FT";
  }
  if (jobType.startsWith("EJECT")) {
    return "EJ";
  }
  return jobType.slice(0, 2);
}

function formatJobSubtitle(job) {
  const targetCount = (job.targets || []).length;
  if (job.type === "flash" && targetCount) {
    return `Flash workflow - ${targetCount} target card${targetCount === 1 ? "" : "s"}`;
  }
  if (job.type === "golden") {
    return "Backup capture and image shrink";
  }
  if (job.type === "wipe") {
    return "FAT32 reformat and staging";
  }
  if (job.type === "eject" || job.type === "eject-all") {
    return "Safe removal sequence";
  }
  return job.phase || "Activity";
}

function activityProgressLabel(job) {
  if (job.type === "flash") {
    return "Sequential Write Progress";
  }
  if (job.type === "golden") {
    return "Backup Image Progress";
  }
  if (job.type === "wipe") {
    return "Reformat Progress";
  }
  return "Activity Progress";
}

function formatStatusLabel(status) {
  const value = String(status || "queued").replace(/_/g, " ");
  if (value === "running") {
    return "In Progress";
  }
  return value.replace(/\b\w/g, (character) => character.toUpperCase());
}

function formatLogTime(timestamp) {
  if (!timestamp) {
    return "--:--:--";
  }
  return new Date(timestamp * 1000).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function currentDestination() {
  return appState.destinationDir || appState.snapshot?.system.default_destination_dir || "";
}

async function postJson(url, payload = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Request failed.");
  }
  return data;
}

function loadExpandedLogs() {
  try {
    return JSON.parse(localStorage.getItem("sd_dashboard_expanded_logs") || "{}");
  } catch (_error) {
    return {};
  }
}

function loadFrozenLogs() {
  try {
    return JSON.parse(localStorage.getItem("sd_dashboard_frozen_logs") || "{}");
  } catch (_error) {
    return {};
  }
}

function persistExpandedLogs() {
  localStorage.setItem(
    "sd_dashboard_expanded_logs",
    JSON.stringify(appState.expandedLogs)
  );
}

function persistFrozenLogs() {
  localStorage.setItem(
    "sd_dashboard_frozen_logs",
    JSON.stringify(appState.frozenLogs)
  );
}

async function copyTextToClipboard(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const helper = document.createElement("textarea");
  helper.value = text;
  helper.setAttribute("readonly", "readonly");
  helper.style.position = "fixed";
  helper.style.top = "-9999px";
  document.body.appendChild(helper);
  helper.select();
  const succeeded = document.execCommand("copy");
  document.body.removeChild(helper);
  if (!succeeded) {
    throw new Error("Clipboard access failed.");
  }
}

function syncSystemLogBlock() {
  window.requestAnimationFrame(() => {
    const block = document.querySelector(".system-log-block[data-job-log]");
    if (!block) {
      return;
    }
    if (systemLogFrozen()) {
      const savedScrollTop = appState.logScrollTop[SYSTEM_LOG_KEY];
      block.scrollTop = Number.isFinite(savedScrollTop) ? savedScrollTop : block.scrollHeight;
      return;
    }
    block.scrollTop = block.scrollHeight;
    appState.logScrollTop[SYSTEM_LOG_KEY] = block.scrollTop;
  });
}

function pill(label, tone) {
  return `<span class="pill ${tone}">${escapeHtml(label)}</span>`;
}

function badge(label, tone) {
  return `<span class="badge ${tone}">${escapeHtml(label)}</span>`;
}

function statusTone(status) {
  if (status === "completed") {
    return "ok";
  }
  if (status === "completed_with_errors") {
    return "warn";
  }
  if (status === "failed") {
    return "danger";
  }
  return "info";
}

function usesTimedMultiCardFlashModel(job) {
  return job?.type === "flash" && (job?.targets || []).length > 1;
}

function overallJobProgressPercent(job, nowTimestamp) {
  if (!usesTimedMultiCardFlashModel(job)) {
    return progressPercent(job);
  }

  if (job.status === "completed" || job.status === "completed_with_errors") {
    return 100;
  }

  const elapsedSeconds = jobElapsedSeconds(job, nowTimestamp);
  if (elapsedSeconds == null) {
    return 0;
  }
  const baselinePercent = (elapsedSeconds / MULTI_CARD_FLASH_BASELINE_SECONDS) * 100;
  const cappedRunningPercent =
    job.status === "failed" ? baselinePercent : Math.min(baselinePercent, 99.4);
  return clampPercent(cappedRunningPercent);
}

function progressPercent(item) {
  const metricPercent = item?.metrics?.percent;
  if (metricPercent != null && Number.isFinite(Number(metricPercent))) {
    return clampPercent(metricPercent);
  }
  return clampPercent(item?.progress || 0);
}

function clampPercent(value) {
  return Math.max(0, Math.min(100, Number(value) || 0));
}

function formatMetrics(metrics) {
  if (!metrics || !metrics.total_bytes) {
    return "";
  }

  const parts = [];
  if (metrics.processed_bytes != null && metrics.total_bytes != null) {
    parts.push(`${formatBytes(metrics.processed_bytes)} / ${formatBytes(metrics.total_bytes)}`);
  }
  if (metrics.speed_bps) {
    parts.push(`${formatBytes(metrics.speed_bps)}/s`);
  }
  return parts.join(" | ");
}

function formatBytes(value) {
  if (value == null) {
    return "n/a";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let number = Number(value);
  let unitIndex = 0;
  while (number >= 1024 && unitIndex < units.length - 1) {
    number /= 1024;
    unitIndex += 1;
  }
  if (unitIndex === 0) {
    return `${Math.round(number)} ${units[unitIndex]}`;
  }
  return `${number.toFixed(1)} ${units[unitIndex]}`;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remaining = total % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${remaining}s`;
  }
  return `${remaining}s`;
}

function formatPercentValue(value) {
  if (value == null || !Number.isFinite(Number(value))) {
    return "n/a";
  }
  return `${Number(value).toFixed(1)}%`;
}

function formatMemoryLine(usedBytes, totalBytes) {
  if (usedBytes == null || totalBytes == null) {
    return "n/a";
  }
  return `${formatBytes(usedBytes)} / ${formatBytes(totalBytes)}`;
}

function formatMemoryMeta(percent, totalBytes) {
  const percentLabel = formatPercentValue(percent);
  if (totalBytes == null) {
    return percentLabel;
  }
  return `${percentLabel} of ${formatBytes(totalBytes)}`;
}

function formatRate(bytesPerSecond) {
  if (bytesPerSecond == null || !Number.isFinite(Number(bytesPerSecond))) {
    return "n/a";
  }
  return `${formatBytes(bytesPerSecond)}/s`;
}

function formatDateTime(timestamp) {
  if (!timestamp) {
    return "n/a";
  }
  return new Date(timestamp * 1000).toLocaleString();
}

function formatElapsed(job, nowTimestamp) {
  const elapsedSeconds = jobElapsedSeconds(job, nowTimestamp);
  if (elapsedSeconds == null) {
    return "n/a";
  }
  return formatDuration(elapsedSeconds);
}

function jobElapsedSeconds(job, nowTimestamp) {
  const startedAt = job?.started_at || job?.created_at;
  const endedAt = job?.finished_at || nowTimestamp;
  if (!startedAt || !endedAt) {
    return null;
  }
  return Math.max(0, endedAt - startedAt);
}

function formatTimedMultiCardFlashRemaining(job, nowTimestamp) {
  if (job.status === "completed" || job.status === "completed_with_errors") {
    return "Done";
  }
  if (job.status === "failed") {
    return "Stopped";
  }

  const elapsedSeconds = jobElapsedSeconds(job, nowTimestamp);
  if (elapsedSeconds == null) {
    return formatDuration(MULTI_CARD_FLASH_BASELINE_SECONDS);
  }

  const remainingSeconds = MULTI_CARD_FLASH_BASELINE_SECONDS - elapsedSeconds;
  if (remainingSeconds <= 0) {
    return "Finalizing";
  }
  return formatDuration(remainingSeconds);
}

function fileName(path) {
  return path.split("/").pop();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function setInputValueIfIdle(element, value) {
  if (document.activeElement !== element) {
    element.value = value || "";
  }
}
