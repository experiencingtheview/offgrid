// The setup page is deliberately small and dependency-free.  It never needs a
// terminal: every action is a button, and the local Python server does the work.
(() => {
  const statusDot = document.getElementById("status-dot");
  const statusTitle = document.getElementById("status-title");
  const statusText = document.getElementById("status-text");
  const progressWrap = document.getElementById("progress-wrap");
  const progressTrack = document.getElementById("progress-track");
  const progressBar = document.getElementById("progress-bar");
  const progressLabel = document.getElementById("progress-label");
  const errorBox = document.getElementById("error");
  const install = document.getElementById("install");
  const open = document.getElementById("open");
  const retry = document.getElementById("retry");
  const recheck = document.getElementById("recheck");
  const workspace = document.getElementById("workspace");

  let redirecting = false;
  let busy = false;

  async function api(path, method) {
    const response = await fetch(path, {
      method: method || "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = body && body.error && body.error.message;
      throw new Error(message || "The setup service did not answer.");
    }
    return body;
  }

  function titleFor(status) {
    const messages = {
      checking: "Checking this computer…",
      "needs-model": "One free download is needed",
      preparing: "Getting things ready…",
      engine: "Downloading the AI engine…",
      downloading: "Downloading the AI model…",
      loading: "Starting the AI…",
      ready: "OffGrid is ready",
      error: "Setup needs attention",
    };
    return messages[status.stage] || "Working on it…";
  }

  function render(status) {
    statusTitle.textContent = titleFor(status);
    statusText.textContent = status.message || "";
    statusDot.className = "status-dot";
    if (status.stage === "ready") statusDot.classList.add("ready");
    if (status.stage === "error") statusDot.classList.add("error");
    if (status.workspace) workspace.textContent = status.workspace;

    const progress = status.progress || {};
    const total = Number(progress.total) || 0;
    const done = Number(progress.done) || 0;
    const showProgress = ["engine", "downloading", "loading", "preparing"].includes(status.stage);
    progressWrap.hidden = !showProgress;
    if (showProgress) {
      if (total > 0) {
        const percent = Math.max(0, Math.min(100, Number(progress.percent) || Math.floor((done / total) * 100)));
        progressTrack.classList.remove("indeterminate");
        progressTrack.setAttribute("aria-valuenow", String(percent));
        progressBar.style.width = percent + "%";
        progressLabel.textContent = percent + "%" + (progress.label ? " · " + progress.label : "");
      } else {
        progressTrack.classList.add("indeterminate");
        progressTrack.removeAttribute("aria-valuenow");
        progressBar.style.width = "35%";
        progressLabel.textContent = "This can take a minute.";
      }
    }

    const isReady = Boolean(status.ready && status.studio_url);
    const isError = status.stage === "error";
    errorBox.hidden = !isError;
    errorBox.textContent = isError ? (status.error || status.message || "Please try again.") : "";
    install.hidden = isReady || isError || busy || ["preparing", "engine", "downloading", "loading"].includes(status.stage);
    open.hidden = !isReady;
    retry.hidden = !isError;
    recheck.disabled = busy;

    if (isReady && !redirecting) {
      redirecting = true;
      statusText.textContent = "Opening OffGrid…";
      window.setTimeout(() => {
        window.location.href = status.studio_url;
      }, 700);
    }
  }

  async function refresh() {
    try {
      const status = await api("/api/status");
      render(status);
      return status;
    } catch (error) {
      statusDot.className = "status-dot error";
      statusTitle.textContent = "Setup is not responding";
      statusText.textContent = String(error.message || error);
      errorBox.hidden = false;
      errorBox.textContent = "Close this page and run OffGrid again. The local setup service may have stopped.";
      install.hidden = true;
      retry.hidden = true;
      return null;
    }
  }

  async function installModel() {
    if (busy) return;
    busy = true;
    install.disabled = true;
    install.textContent = "Starting setup…";
    try {
      render(await api("/api/install", "POST"));
    } catch (error) {
      errorBox.hidden = false;
      errorBox.textContent = String(error.message || error);
      retry.hidden = false;
      install.hidden = true;
    } finally {
      busy = false;
      install.disabled = false;
      install.textContent = "Set up OffGrid · about 2 GB";
    }
  }

  async function recheckServers() {
    busy = true;
    recheck.disabled = true;
    try {
      render(await api("/api/recheck", "POST"));
    } catch (error) {
      errorBox.hidden = false;
      errorBox.textContent = String(error.message || error);
    } finally {
      busy = false;
      recheck.disabled = false;
    }
  }

  async function openStudio() {
    try {
      const result = await api("/api/open", "POST");
      if (result.studio_url) {
        window.location.href = result.studio_url;
      }
    } catch (error) {
      errorBox.hidden = false;
      errorBox.textContent = String(error.message || error);
    }
  }

  install.addEventListener("click", installModel);
  retry.addEventListener("click", installModel);
  recheck.addEventListener("click", recheckServers);
  open.addEventListener("click", openStudio);

  refresh();
  window.setInterval(refresh, 1000);
})();
