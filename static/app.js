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

  // "Upload a file" vs "Import from link" tabs
  const modeTabFile = document.getElementById("modeTabFile");
  const modeTabLink = document.getElementById("modeTabLink");
  const fileModePanel = document.getElementById("fileModePanel");
  const linkModePanel = document.getElementById("linkModePanel");
  const importUrlInput = document.getElementById("importUrlInput");
  const importBtn = document.getElementById("importBtn");

  // Icon rail mirrors the two tabs
  const railUpload = document.getElementById("railUpload");
  const railLink = document.getElementById("railLink");

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

  // Switch between the two ways of adding a video
  function setMode(mode) {
    const isFile = mode === "file";
    modeTabFile.classList.toggle("active", isFile);
    modeTabLink.classList.toggle("active", !isFile);
    modeTabFile.setAttribute("aria-selected", isFile ? "true" : "false");
    modeTabLink.setAttribute("aria-selected", isFile ? "false" : "true");
    fileModePanel.style.display = isFile ? "block" : "none";
    linkModePanel.style.display = isFile ? "none" : "block";
    if (railUpload) railUpload.classList.toggle("is-active", isFile);
    if (railLink) railLink.classList.toggle("is-active", !isFile);
    uploadProgressBox.style.display = "none";
    hideError();
    if (!isFile) {
      importUrlInput.focus();
    }
  }

  modeTabFile.addEventListener("click", () => setMode("file"));
  modeTabLink.addEventListener("click", () => setMode("link"));
  if (railUpload) railUpload.addEventListener("click", () => setMode("file"));
  if (railLink) railLink.addEventListener("click", () => setMode("link"));

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
});
