/* OffGrid Studio: title, team badge and guided home setup, independent of the chat bundle. */
(() => {
  const TITLE = "OffGrid Studio";

  const keepTitle = () => {
    if (document.title !== TITLE) document.title = TITLE;
  };
  const titleElement = document.querySelector("title");
  if (titleElement && window.MutationObserver) {
    new MutationObserver(keepTitle).observe(titleElement, { childList: true });
  }
  keepTitle();

  const badge = document.getElementById("offgrid-badge");
  const panel = document.getElementById("offgrid-team");
  const toggle = document.getElementById("offgrid-team-toggle");
  const status = document.getElementById("offgrid-status");
  const summary = document.getElementById("offgrid-summary");
  if (!badge || !panel || !toggle) return;

  const homePanel = document.getElementById("offgrid-home");
  const homeToggle = document.getElementById("offgrid-home-toggle");
  const homeStatus = document.getElementById("offgrid-home-status");
  const homeConnected = document.getElementById("offgrid-home-connected");
  const homeConnectedSummary = document.getElementById("offgrid-home-connected-summary");
  const homeAdd = document.getElementById("offgrid-home-add");
  const homeForm = document.getElementById("offgrid-home-form");
  const homeUrl = document.getElementById("offgrid-home-url");
  const homeToken = document.getElementById("offgrid-home-token");
  const homeScan = document.getElementById("offgrid-home-scan");
  const homeError = document.getElementById("offgrid-home-error");
  const homeDevices = document.getElementById("offgrid-home-devices");
  const homeFound = document.getElementById("offgrid-home-found");
  const homeEntities = document.getElementById("offgrid-home-entities");
  const homeName = document.getElementById("offgrid-home-name");
  const homeControl = document.getElementById("offgrid-home-control");
  const homeSave = document.getElementById("offgrid-home-save");
  const homeBack = document.getElementById("offgrid-home-back");
  const homeSuccess = document.getElementById("offgrid-home-success");

  let loadingState = null;
  let lastPanelMarkup = "";
  let homeState = null;

  const setLoading = (loading) => {
    if (loadingState === loading) return;
    loadingState = loading;
    badge.setAttribute("aria-busy", String(loading));
    if (!summary) return;
    summary.classList.toggle("offgrid-skeleton", loading);
    if (loading) summary.setAttribute("aria-hidden", "true");
    else summary.removeAttribute("aria-hidden");
  };

  const setText = (element, value) => {
    if (element && element.textContent !== value) element.textContent = value;
  };

  const setPanel = (markup) => {
    if (lastPanelMarkup === markup) return;
    lastPanelMarkup = markup;
    panel.innerHTML = markup;
  };

  const setStatus = (text, className) => {
    if (!status) return;
    if (status.textContent !== text) status.textContent = text;
    if (status.className !== className) status.className = className;
  };

  const escape = (value) =>
    String(value == null ? "" : value).replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[character]));

  const setHomeError = (message) => {
    if (!homeError) return;
    setText(homeError, message || "");
    if (message) homeError.removeAttribute("hidden");
    else homeError.setAttribute("hidden", "");
  };

  const setHomeSuccess = (message) => {
    if (!homeSuccess) return;
    setText(homeSuccess, message || "");
    if (message) homeSuccess.removeAttribute("hidden");
    else homeSuccess.setAttribute("hidden", "");
  };

  const busy = (button, active, label) => {
    if (!button) return;
    button.disabled = active;
    if (active) {
      button.dataset.label = button.textContent;
      button.textContent = label;
    } else if (button.dataset.label) {
      button.textContent = button.dataset.label;
      delete button.dataset.label;
    }
  };

  toggle.addEventListener("click", () => {
    const opening = panel.hasAttribute("hidden");
    if (opening) panel.removeAttribute("hidden");
    else panel.setAttribute("hidden", "");
    toggle.setAttribute("aria-expanded", String(opening));
    if (opening && homePanel && homeToggle) {
      homePanel.setAttribute("hidden", "");
      homeToggle.setAttribute("aria-expanded", "false");
    }
  });

  const load = async () => {
    try {
      const response = await fetch("/v1/offgrid/info", {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const info = await response.json();
      const agents = Array.isArray(info.agents) ? info.agents : [];
      const rows = agents.map((agent) =>
        "<li><b>" + escape(agent.name) + "</b> <span>" + escape(agent.title) + "</span>" +
        (agent.custom ? " <em>custom</em>" : "") + "</li>"
      ).join("");
      setLoading(false);
      setText(summary, (info.model || "auto") + " · " + agents.length + " agents");
      setPanel(
        '<p class="offgrid-workspace">' + escape(info.workspace || "") + "</p>" +
        "<ul>" + rows + "</ul>" +
        (info.warnings && info.warnings.length
          ? '<p class="offgrid-warning">' + escape(info.warnings.join(" ")) + "</p>"
          : "")
      );
      setStatus("ready", "offgrid-status offgrid-ready");
    } catch (error) {
      setLoading(false);
      setText(summary, "start a local model server to chat");
      setPanel(
        '<p class="offgrid-warning">No model answered. Start llama-server, Ollama or LM Studio, ' +
        "then reload this page.</p>"
      );
      setStatus("model offline", "offgrid-status offgrid-offline");
    }
  };

  const postJson = async (path, body) => {
    const response = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    if (!response.ok) {
      const message = data && data.error && data.error.message;
      throw new Error(message || ("HTTP " + response.status));
    }
    return data || {};
  };

  const renderHomeStatus = (data) => {
    if (!data) return;
    homeState = data;
    const connected = Boolean(data.connected);
    setText(homeStatus, connected ? "connected" : "not connected");
    if (homeConnected) {
      if (connected) homeConnected.removeAttribute("hidden");
      else homeConnected.setAttribute("hidden", "");
    }
    if (homeForm) {
      if (connected) homeForm.setAttribute("hidden", "");
      else homeForm.removeAttribute("hidden");
    }
    if (!connected && homeDevices) homeDevices.setAttribute("hidden", "");
    if (connected && homeConnectedSummary) {
      const devices = Array.isArray(data.devices) ? data.devices : [];
      const policy = data.policy === "auto"
        ? "control allowed"
        : data.policy === "ask"
          ? "asks before switching"
          : "read-only";
      const rows = devices.map((device) =>
        "<div><b>" + escape(device.name) + "</b> · " +
        escape(String(device.points || 0)) + " device(s) · " +
        escape(device.url || "") + "</div>"
      ).join("");
      homeConnectedSummary.innerHTML =
        rows + '<div class="offgrid-home-hint">Policy: ' + escape(policy) + "</div>";
    }
    const defaults = data.defaults || {};
    if (homeUrl && !homeUrl.value) homeUrl.value = defaults.url || "http://homeassistant.local:8123";
    if (homeName && !homeName.value) homeName.value = defaults.name || "home";
  };

  const loadHome = async () => {
    if (!homePanel) return;
    try {
      const response = await fetch("/v1/offgrid/home", {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      renderHomeStatus(await response.json());
      setHomeError("");
    } catch (error) {
      setText(homeStatus, "unavailable");
      setHomeError("Home setup is not available in this Studio.");
    }
  };

  const renderEntities = (entities) => {
    if (!homeEntities) return;
    const groups = {};
    entities.forEach((entity) => {
      const group = entity.group || "Devices";
      if (!groups[group]) groups[group] = [];
      groups[group].push(entity);
    });
    const order = ["Lights & switches", "Sensors", "Numbers"];
    const names = Object.keys(groups).sort((a, b) => {
      const left = order.indexOf(a);
      const right = order.indexOf(b);
      if (left === -1 && right === -1) return a.localeCompare(b);
      if (left === -1) return 1;
      if (right === -1) return -1;
      return left - right;
    });
    homeEntities.innerHTML = names.map((name) => {
      const rows = groups[name].map((entity) => {
        const checked = entity.selected ? " checked" : "";
        const value = entity.state || "no value";
        const kind = entity.writable ? "switchable" : "read-only";
        return '<label class="offgrid-entity"><input type="checkbox" data-entity="' +
          escape(entity.entity_id) + '"' + checked + "><span><b>" +
          escape(entity.name) + "</b><small>" + escape(value) + " · " +
          escape(entity.entity_id) + " · " + kind + "</small></span></label>";
      }).join("");
      return '<div class="offgrid-home-group">' + escape(name) + "</div>" + rows;
    }).join("");
  };

  const showSetup = () => {
    if (homeConnected) homeConnected.setAttribute("hidden", "");
    if (homeForm) homeForm.removeAttribute("hidden");
    if (homeDevices) homeDevices.setAttribute("hidden", "");
    setHomeSuccess("");
    setHomeError("");
    setText(homeStatus, "not connected");
    if (homeToken) homeToken.value = "";
    if (homeControl) homeControl.checked = false;
    if (homeFound) setText(homeFound, "");
    if (homeName) {
      const count = homeState && Array.isArray(homeState.devices)
        ? homeState.devices.length
        : 0;
      homeName.value = count ? "home" + (count + 1) : "home";
    }
    if (homeUrl) homeUrl.focus();
  };

  if (homeForm && homeScan) {
    homeForm.addEventListener("submit", (event) => {
      event.preventDefault();
      homeScan.click();
    });
  }

  if (homePanel && homeToggle) {
    homeToggle.addEventListener("click", () => {
      const opening = homePanel.hasAttribute("hidden");
      if (opening) homePanel.removeAttribute("hidden");
      else homePanel.setAttribute("hidden", "");
      homeToggle.setAttribute("aria-expanded", String(opening));
      if (opening) {
        panel.setAttribute("hidden", "");
        toggle.setAttribute("aria-expanded", "false");
        loadHome();
      }
    });
  }

  if (homeScan) {
    homeScan.addEventListener("click", async () => {
      const url = homeUrl ? homeUrl.value.trim() : "";
      const token = homeToken ? homeToken.value.trim() : "";
      setHomeError("");
      setHomeSuccess("");
      if (!url) {
        setHomeError("Enter the Home Assistant address.");
        return;
      }
      if (!token) {
        setHomeError(
          "Create a long-lived access token in Home Assistant: Profile → Security."
        );
        return;
      }
      busy(homeScan, true, "Testing…");
      try {
        const result = await postJson("/v1/offgrid/home/scan", {
          url: url,
          token: token,
        });
        const entities = Array.isArray(result.entities) ? result.entities : [];
        if (!entities.length) throw new Error("No supported devices were found.");
        renderEntities(entities);
        if (homeFound) {
          setText(
            homeFound,
            result.count + " supported device(s). Uncheck anything you do not want OffGrid to see."
          );
        }
        if (homeForm) homeForm.setAttribute("hidden", "");
        if (homeDevices) homeDevices.removeAttribute("hidden");
        if (result.truncated) {
          setHomeError(
            "Only the first " + entities.length + " devices are shown. You can connect those now and add the rest later."
          );
        }
      } catch (error) {
        setHomeError(error.message || "The connection test failed.");
      } finally {
        busy(homeScan, false);
      }
    });
  }

  if (homeBack) {
    homeBack.addEventListener("click", () => {
      if (homeDevices) homeDevices.setAttribute("hidden", "");
      if (homeForm) homeForm.removeAttribute("hidden");
      setHomeError("");
    });
  }

  if (homeAdd) {
    homeAdd.addEventListener("click", showSetup);
  }

  if (homeSave) {
    homeSave.addEventListener("click", async () => {
      const selected = homeEntities
        ? Array.from(homeEntities.querySelectorAll("input[data-entity]"))
          .filter((input) => input.checked)
          .map((input) => input.getAttribute("data-entity"))
        : [];
      setHomeError("");
      setHomeSuccess("");
      if (!selected.length) {
        setHomeError("Choose at least one device.");
        return;
      }
      busy(homeSave, true, "Connecting…");
      try {
        const result = await postJson("/v1/offgrid/home/connect", {
          url: homeUrl ? homeUrl.value.trim() : "",
          token: homeToken ? homeToken.value.trim() : "",
          name: homeName ? homeName.value.trim() : "home",
          entities: selected,
          allow_control: Boolean(homeControl && homeControl.checked),
        });
        if (homeToken) homeToken.value = "";
        if (homeDevices) homeDevices.setAttribute("hidden", "");
        setHomeSuccess(result.message || "Home connected.");
        renderHomeStatus(result.status);
      } catch (error) {
        setHomeError(error.message || "The home could not be connected.");
      } finally {
        busy(homeSave, false);
      }
    });
  }

  load();
  loadHome();
  window.setInterval(load, 30000);
})();
