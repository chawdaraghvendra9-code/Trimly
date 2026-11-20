/* ============================================================
   Cliptor Clip Studio - Frontend JavaScript (Upload & Polling)
   Simple, clean, beginner-friendly code
   ============================================================ */

document.addEventListener("DOMContentLoaded", () => {
  const uploadForm = document.getElementById("uploadForm");
  const videoFileInput = document.getElementById("videoFileInput");
  const dropZone = document.getElementById("dropZone");
  const uploadBtn = document.getElementById("uploadBtn");
  const fileSelectionInfo = document.getElementById("fileSelectionInfo");
  const uploadProgressBox = document.getElementById("uploadProgressBox");
  const uploadProgressBar = document.getElementById("uploadProgressBar");
  const uploadStatusText = document.getElementById("uploadStatusText");
  const errorMessageAlert = document.getElementById("errorMessageAlert");
  const errorMessageText = document.getElementById("errorMessageText");

  // "Upload a file", "Import from link" and "Clip a live stream" tabs
  const modeTabFile = document.getElementById("modeTabFile");
  const modeTabLink = document.getElementById("modeTabLink");
  const modeTabLive = document.getElementById("modeTabLive");
  const fileModePanel = document.getElementById("fileModePanel");
  const linkModePanel = document.getElementById("linkModePanel");
  const liveModePanel = document.getElementById("liveModePanel");
  const importUrlInput = document.getElementById("importUrlInput");
  const importBtn = document.getElementById("importBtn");
  const liveUrlInput = document.getElementById("liveUrlInput");
  const liveSecondsInput = document.getElementById("liveSecondsInput");
  const liveBtn = document.getElementById("liveBtn");

  // Icon rail mirrors the three tabs
  const railUpload = document.getElementById("railUpload");
  const railLink = document.getElementById("railLink");
  const railLive = document.getElementById("railLive");

  // Helper to show error banner
  function showError(msg) {
    errorMessageText.textContent = msg;
    errorMessageAlert.style.display = "flex";
  }

  // Helper to hide error banner
  function hideError() {
    errorMessageAlert.style.display = "none";
    errorMessageText.textContent = "";
  }

  // Switch between the three ways of adding a video
  function setMode(mode) {
    const modes = ["file", "link", "live"];
    const panels = { file: fileModePanel, link: linkModePanel, live: liveModePanel };
    const tabs = { file: modeTabFile, link: modeTabLink, live: modeTabLive };
    const rails = { file: railUpload, link: railLink, live: railLive };

    modes.forEach((name) => {
      const isCurrent = name === mode;
      tabs[name].classList.toggle("active", isCurrent);
      tabs[name].setAttribute("aria-selected", isCurrent ? "true" : "false");
      panels[name].style.display = isCurrent ? "block" : "none";
      if (rails[name]) rails[name].classList.toggle("is-active", isCurrent);
    });

    uploadProgressBox.style.display = "none";
    hideError();
    if (mode === "link") importUrlInput.focus();
    if (mode === "live") liveUrlInput.focus();
  }

  modeTabFile.addEventListener("click", () => setMode("file"));
  modeTabLink.addEventListener("click", () => setMode("link"));
  modeTabLive.addEventListener("click", () => setMode("live"));
  if (railUpload) railUpload.addEventListener("click", () => setMode("file"));
  if (railLink) railLink.addEventListener("click", () => setMode("link"));
  if (railLive) railLive.addEventListener("click", () => setMode("live"));

  // Upload limit comes from the server so the two never disagree.
  const maxSizeMB = parseFloat(uploadForm.dataset.maxSizeMb || "0");

  // Handle file input change
  videoFileInput.addEventListener("change", () => {
    hideError();

    if (!videoFileInput.files.length) {
      fileSelectionInfo.style.display = "none";
      dropZone.classList.remove("has-file");
      uploadBtn.disabled = true;
      return;
    }

    const file = videoFileInput.files[0];
    const sizeMB = file.size / (1024 * 1024);
    fileSelectionInfo.textContent = `Selected: ${file.name} (${sizeMB.toFixed(1)} MB)`;
    fileSelectionInfo.style.display = "inline-block";
    dropZone.classList.add("has-file");

    // Stop a too-large file before the whole upload is wasted.
    if (maxSizeMB > 0 && sizeMB > maxSizeMB) {
      showError(`That file is ${sizeMB.toFixed(1)} MB. The limit is ${maxSizeMB} MB.`);
      uploadBtn.disabled = true;
      return;
    }

    uploadBtn.disabled = false;
  });

  // Drag and drop events for dropZone
  ["dragenter", "dragover"].forEach(eventName => {
    dropZone.addEventListener(eventName, (e) => {
      e.preventDefault();
      dropZone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach(eventName => {
    dropZone.addEventListener(eventName, (e) => {
      e.preventDefault();
      dropZone.classList.remove("dragover");
    });
  });

  dropZone.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length > 0) {
      videoFileInput.files = e.dataTransfer.files;
      videoFileInput.dispatchEvent(new Event("change"));
    }
  });

  // Handle form submission using XMLHttpRequest so we get upload progress
  uploadForm.addEventListener("submit", (e) => {
    e.preventDefault();
    hideError();

    if (!videoFileInput.files || videoFileInput.files.length === 0) {
      showError("Please choose a video file first.");
      return;
    }

    const file = videoFileInput.files[0];
    const cropStyleSelect = document.getElementById("cropStyleSelect");
    const formData = new FormData();
    formData.append("video_file", file);
    if (cropStyleSelect) {
      formData.append("crop_style", cropStyleSelect.value);
    }

    uploadBtn.disabled = true;
    uploadProgressBox.style.display = "block";
    uploadProgressBar.style.width = "0%";
    uploadStatusText.textContent = "Uploading video... 0%";

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload", true);

    // Track upload progress
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        const percent = Math.round((event.loaded / event.total) * 100);
        uploadProgressBar.style.width = `${percent}%`;
        uploadStatusText.textContent = `Uploading video... ${percent}%`;
      }
    };

    xhr.onload = () => {
      if (xhr.status === 200) {
        try {
          const resp = JSON.parse(xhr.responseText);
          uploadStatusText.textContent = "Upload complete! Redirecting to studio...";
          uploadProgressBar.style.width = "100%";
          // Redirect to the editor view for this job
          setTimeout(() => {
            window.location.href = `/jobs/${resp.job_id}/view`;
          }, 800);
        } catch (err) {
          showError("Unexpected response from server.");
          uploadBtn.disabled = false;
        }
      } else {
        try {
          const errResp = JSON.parse(xhr.responseText);
          showError(errResp.detail || "Upload failed. Please try again.");
        } catch (_) {
          showError(`Upload failed with status code ${xhr.status}.`);
        }
        uploadBtn.disabled = false;
        uploadProgressBox.style.display = "none";
      }
    };

    xhr.onerror = () => {
      showError("A network error occurred during upload. Please check your connection.");
      uploadBtn.disabled = false;
      uploadProgressBox.style.display = "none";
    };

    xhr.send(formData);
  });

  // Import from a direct video link, or a public Google Drive / Dropbox share link.
  // The server validates the link first, then downloads it in the background.
  async function startLinkImport() {
    hideError();

    const url = (importUrlInput.value || "").trim();
    if (!url) {
      showError("Please paste a link first.");
      importUrlInput.focus();
      return;
    }

    const cropStyleSelect = document.getElementById("cropStyleSelect");
    const cropStyle = cropStyleSelect ? cropStyleSelect.value : "blurred_background";

    importBtn.disabled = true;
    importBtn.textContent = "⏳ Starting download...";

    try {
      const resp = await fetch("/api/import-link", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url, crop_style: cropStyle })
      });
      const data = await resp.json().catch(() => ({}));

      if (!resp.ok) {
        // The server sends a friendly explanation, for example for streaming sites.
        showError(data.detail || "That link could not be imported.");
        return;
      }

      // Download runs in the background - the studio page shows its progress.
      uploadProgressBox.style.display = "block";
      uploadProgressBar.style.width = "2%";
      uploadStatusText.textContent = "Link accepted. Starting the download...";
      window.location.href = `/jobs/${data.job_id}/view`;
    } catch (err) {
      showError("Could not reach the server. Make sure it is still running, then try again.");
    } finally {
      importBtn.disabled = false;
      importBtn.textContent = "Import from link";
    }
  }

  importBtn.addEventListener("click", startLinkImport);

  // Pressing Enter in the link box starts the import
  importUrlInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      startLinkImport();
    }
  });

  // Record a short window from a live stream, then let the pipeline clip it.
  async function startLiveCapture() {
    hideError();

    const url = (liveUrlInput.value || "").trim();
    if (!url) {
      showError("Please paste the live stream link first.");
      liveUrlInput.focus();
      return;
    }

    // Stay inside the limits the server also enforces.
    const minSeconds = parseInt(liveSecondsInput.dataset.min || "20", 10);
    const maxSeconds = parseInt(liveSecondsInput.dataset.max || "600", 10);
    const fallback = parseInt(liveSecondsInput.dataset.default || "90", 10);
    let seconds = parseInt(liveSecondsInput.value || "", 10);
    if (Number.isNaN(seconds)) seconds = fallback;
    seconds = Math.min(maxSeconds, Math.max(minSeconds, seconds));
    liveSecondsInput.value = seconds;

    const cropStyleSelect = document.getElementById("cropStyleSelect");
    const cropStyle = cropStyleSelect ? cropStyleSelect.value : "blurred_background";

    liveBtn.disabled = true;
    liveBtn.textContent = "⏳ Connecting...";

    try {
      const resp = await fetch("/api/live", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url, seconds: seconds, crop_style: cropStyle })
      });
      const data = await resp.json().catch(() => ({}));

      if (!resp.ok) {
        showError(data.detail || "That live stream could not be recorded.");
        return;
      }

      // Recording runs in the background - the studio page shows its progress.
      uploadProgressBox.style.display = "block";
      uploadProgressBar.style.width = "2%";
      uploadStatusText.textContent =
        `Recording ${data.capture_seconds} seconds of the live stream...`;
      window.location.href = `/jobs/${data.job_id}/view`;
    } catch (err) {
      showError("Could not reach the server. Make sure it is still running, then try again.");
    } finally {
      liveBtn.disabled = false;
      liveBtn.textContent = "Record & clip";
    }
  }

  liveBtn.addEventListener("click", startLiveCapture);

  liveUrlInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      startLiveCapture();
    }
  });

  // One-time "runs on your computer" notice.
  const welcomeModal = document.getElementById("welcomeModal");
  if (welcomeModal) {
    const welcomeOkBtn = document.getElementById("welcomeOkBtn");
    let welcomeClosed = false;

    function closeWelcome() {
      if (welcomeClosed) return;
      welcomeClosed = true;
      welcomeModal.remove();
      // Remember the choice, so the notice never shows again.
      fetch("/api/welcome/seen", { method: "POST" }).catch(() => {});
    }

    welcomeOkBtn.addEventListener("click", closeWelcome);
    welcomeModal.addEventListener("click", (event) => {
      if (event.target === welcomeModal) closeWelcome();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeWelcome();
    });
  }
});
