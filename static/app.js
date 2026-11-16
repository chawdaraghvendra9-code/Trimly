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

  // Handle file input change
  videoFileInput.addEventListener("change", () => {
    hideError();
    if (videoFileInput.files.length > 0) {
      const file = videoFileInput.files[0];
      const sizeMB = (file.size / (1024 * 1024)).toFixed(1);
      fileSelectionInfo.textContent = `Selected: ${file.name} (${sizeMB} MB)`;
      fileSelectionInfo.style.display = "inline-block";
      uploadBtn.disabled = false;
    } else {
      fileSelectionInfo.style.display = "none";
      uploadBtn.disabled = true;
    }
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
});
