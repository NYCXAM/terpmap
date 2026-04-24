const mapEl = document.querySelector("#map");
const pinPanel = document.querySelector("#pinPanel");
const closePinPanel = document.querySelector("#closePinPanel");
const form = document.querySelector("#reportForm");
const pinStatus = document.querySelector("#pinStatus");
const locationStatus = document.querySelector("#locationStatus");
const feedItems = document.querySelector("#feedItems");
const searchInput = document.querySelector("#searchInput");
const windowSelect = document.querySelector("#windowSelect");
const categoryFilter = document.querySelector("#categoryFilter");
const activeFilters = document.querySelector("#activeFilters");
const summaryButton = document.querySelector("#summaryButton");
const summaryStatus = document.querySelector("#summaryStatus");
const summaryOutput = document.querySelector("#summaryOutput");
const mergeButton = document.querySelector("#mergeButton");
const clearMergeButton = document.querySelector("#clearMergeButton");
const mergeStatus = document.querySelector("#mergeStatus");
const imageInput = document.querySelector("#imageInput");
const imagePreview = document.querySelector("#imagePreview");
const imagePreviewWrap = document.querySelector("#imagePreviewWrap");
const clearImageButton = document.querySelector("#clearImageButton");
const aiEnabled = document.body.dataset.aiEnabled === "true";

const center = [Number(mapEl.dataset.lat), Number(mapEl.dataset.lng)];
const map = L.map("map", { zoomControl: true }).setView(center, 16);
const reportLayer = L.layerGroup().addTo(map);

const state = {
  category: "",
  pendingImageData: "",
  reports: [],
  search: "",
  selectedMergeIds: [],
  summary: "",
  window: "week",
};

const windowLabels = {
  today: "today",
  week: "this week",
  two_weeks: "the past two weeks",
  month: "the past month",
};

let selectedMarker = null;
let locationMarker = null;
let searchDebounceHandle = 0;

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

const selectedIcon = L.divIcon({
  className: "selected-pin",
  iconSize: [18, 18],
  iconAnchor: [9, 9],
});

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatTime(epochSeconds) {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(epochSeconds * 1000));
}

function severityColor(severity) {
  if (severity === "High") return "#c8102e";
  if (severity === "Medium") return "#b77900";
  return "#1d7f63";
}

function choosePin(latlng) {
  form.lat.value = latlng.lat.toFixed(6);
  form.lng.value = latlng.lng.toFixed(6);
  pinStatus.textContent = `Selected ${form.lat.value}, ${form.lng.value}`;
  pinPanel.classList.remove("hidden");

  if (selectedMarker) {
    selectedMarker.setLatLng(latlng);
  } else {
    selectedMarker = L.marker(latlng, { icon: selectedIcon }).addTo(map);
  }
}

function closePinComposer() {
  pinPanel.classList.add("hidden");
  if (selectedMarker) {
    selectedMarker.remove();
    selectedMarker = null;
  }
  form.lat.value = "";
  form.lng.value = "";
  pinStatus.textContent = "Click the map to choose where the pin should go.";
}

function clearImageSelection() {
  state.pendingImageData = "";
  imageInput.value = "";
  imagePreview.removeAttribute("src");
  imagePreviewWrap.classList.add("hidden");
}

function showImagePreview(dataUrl) {
  imagePreview.src = dataUrl;
  imagePreviewWrap.classList.remove("hidden");
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Image could not be read."));
    reader.readAsDataURL(file);
  });
}

function currentFilterParams() {
  return {
    window: state.window,
    category: state.category,
    search: state.search.trim(),
  };
}

function updateActiveFilterText() {
  const categoryLabel = state.category || "all categories";
  activeFilters.textContent = `Showing events from ${windowLabels[state.window]} across ${categoryLabel}.`;
}

function updateMergeControls() {
  mergeButton.disabled = state.selectedMergeIds.length !== 2;
  clearMergeButton.disabled = state.selectedMergeIds.length === 0;

  if (state.selectedMergeIds.length === 0) {
    mergeStatus.textContent = `Showing events from ${windowLabels[state.window]}. Select two cards to merge duplicates.`;
    return;
  }

  if (state.selectedMergeIds.length === 1) {
    mergeStatus.textContent = "Primary event selected. Choose one more duplicate to merge into it.";
    return;
  }

  mergeStatus.textContent = "Ready to merge. The first selected event stays visible and absorbs the second.";
}

function buildPopupHtml(report) {
  const imageHtml = report.image_data
    ? `<img class="popup-image" src="${report.image_data}" alt="Pinned event image">`
    : "";
  const mergedHtml = report.merged_count
    ? `<p class="popup-meta">Merged reports: ${report.merged_count}</p>`
    : "";

  return `
    <div class="popup-card">
      <strong>${escapeHtml(report.title)}</strong>
      <p class="popup-meta">${escapeHtml(report.category)} · ${escapeHtml(report.severity)} · ${escapeHtml(report.location_label)} · Vote score ${report.score}</p>
      <p>${escapeHtml(report.description)}</p>
      ${imageHtml}
      ${mergedHtml}
      <p class="popup-meta">${escapeHtml(report.author_name)} · ${formatTime(report.last_activity_at)}</p>
    </div>
  `;
}

function buildMergedList(report) {
  if (!report.merged_count) {
    return "";
  }

  const items = report.reports
    .filter((item) => item.id !== report.id)
    .map(
      (item) => `
        <li>
          <strong>${escapeHtml(item.title)}</strong>
          <span>${escapeHtml(item.location_label)} · ${formatTime(item.created_at)}</span>
        </li>
      `,
    )
    .join("");

  return `
    <details class="merged-list">
      <summary>${report.merged_count} merged report${report.merged_count === 1 ? "" : "s"}</summary>
      <ul>${items}</ul>
    </details>
  `;
}

function buildReportCard(report, marker) {
  const card = document.createElement("article");
  const isSelected = state.selectedMergeIds.includes(report.id);
  const upActive = report.user_vote > 0 ? "is-active" : "";
  const downActive = report.user_vote < 0 ? "is-active" : "";

  card.className = `feed-card${isSelected ? " is-selected" : ""}`;
  card.dataset.reportId = report.id;
  card.innerHTML = `
    <div class="feed-card-top">
      <div class="feed-card-main">
        <p class="feed-kicker">${escapeHtml(report.category)} · ${escapeHtml(report.severity)}</p>
        <h3>${escapeHtml(report.title)}</h3>
        <p class="feed-location">${escapeHtml(report.location_label)}</p>
        <p>${escapeHtml(report.description)}</p>
      </div>
      <div class="vote-widget">
        <span class="vote-label">Community vote</span>
        <div class="vote-controls">
          <button type="button" class="vote-button ${upActive}" data-action="vote-up">Up</button>
          <strong class="vote-score">${report.score}</strong>
          <button type="button" class="vote-button ${downActive}" data-action="vote-down">Down</button>
        </div>
      </div>
    </div>
    ${report.image_data ? `<img class="feed-image" src="${report.image_data}" alt="Pinned event image">` : ""}
    ${buildMergedList(report)}
    <div class="feed-meta">
      <span>${escapeHtml(report.author_name)}</span>
      <span>${formatTime(report.last_activity_at)}</span>
      <span>${report.merged_count ? `${report.merged_count} merged` : "Single report"}</span>
    </div>
    <div class="feed-actions">
      <button type="button" class="${isSelected ? "secondary-action" : "ghost-action"}" data-action="merge-select">
        ${isSelected ? "Selected for merge" : "Select to merge"}
      </button>
    </div>
  `;

  card.addEventListener("click", async (event) => {
    const actionButton = event.target.closest("[data-action]");
    if (actionButton) {
      const action = actionButton.dataset.action;
      if (action === "vote-up") {
        await submitVote(report, report.user_vote > 0 ? 0 : 1);
      } else if (action === "vote-down") {
        await submitVote(report, report.user_vote < 0 ? 0 : -1);
      } else if (action === "merge-select") {
        toggleMergeSelection(report.id);
      }
      return;
    }

    map.setView([report.lat, report.lng], 18);
    marker.openPopup();
  });

  return card;
}

function renderReports() {
  reportLayer.clearLayers();
  feedItems.innerHTML = "";

  if (!state.reports.length) {
    feedItems.innerHTML = `<article class="empty-state">No events match this view yet.</article>`;
    updateMergeControls();
    return;
  }

  for (const report of state.reports) {
    const marker = L.circleMarker([report.lat, report.lng], {
      radius: 9,
      color: "#28160f",
      weight: 2,
      fillColor: severityColor(report.severity),
      fillOpacity: 0.9,
    }).addTo(reportLayer);

    marker.bindPopup(buildPopupHtml(report));
    feedItems.appendChild(buildReportCard(report, marker));
  }

  updateMergeControls();
}

async function loadReports() {
  const params = new URLSearchParams({ window: state.window });
  if (state.category) {
    params.set("category", state.category);
  }
  if (state.search.trim()) {
    params.set("search", state.search.trim());
  }

  const response = await fetch(`/api/reports?${params.toString()}`);
  if (!response.ok) {
    mergeStatus.textContent = await response.text();
    return;
  }

  state.reports = await response.json();
  const visibleIds = new Set(state.reports.map((report) => report.id));
  state.selectedMergeIds = state.selectedMergeIds.filter((reportId) => visibleIds.has(reportId));
  updateActiveFilterText();
  renderReports();
}

async function submitVote(report, value) {
  const response = await fetch(`/api/reports/${report.id}/vote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });

  if (!response.ok) {
    mergeStatus.textContent = await response.text();
    return;
  }

  await loadReports();
}

function formatSummaryText(text) {
  return escapeHtml(text).replaceAll("\n", "<br>");
}

async function refreshCampusSummary() {
  if (!aiEnabled) {
    summaryStatus.textContent = "AI summaries are unavailable until OPENAI_API_KEY is configured.";
    return;
  }

  summaryButton.disabled = true;
  summaryStatus.textContent = "Reading the current visible campus events...";
  const response = await fetch("/api/summary", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(currentFilterParams()),
  });

  summaryButton.disabled = false;
  if (!response.ok) {
    summaryStatus.textContent = await response.text();
    return;
  }

  const payload = await response.json();
  state.summary = payload.summary;
  summaryOutput.innerHTML = formatSummaryText(payload.summary);
  summaryStatus.textContent = `Summary built from ${payload.count} visible event${payload.count === 1 ? "" : "s"}.`;
}

function toggleMergeSelection(reportId) {
  if (state.selectedMergeIds.includes(reportId)) {
    state.selectedMergeIds = state.selectedMergeIds.filter((id) => id !== reportId);
  } else if (state.selectedMergeIds.length < 2) {
    state.selectedMergeIds = [...state.selectedMergeIds, reportId];
  } else {
    state.selectedMergeIds = [state.selectedMergeIds[1], reportId];
  }

  renderReports();
}

async function mergeSelectedReports() {
  if (state.selectedMergeIds.length !== 2) {
    updateMergeControls();
    return;
  }

  const [targetId, sourceId] = state.selectedMergeIds;
  mergeStatus.textContent = "Merging selected events...";
  const response = await fetch("/api/reports/merge", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_id: targetId, source_id: sourceId }),
  });

  if (!response.ok) {
    mergeStatus.textContent = await response.text();
    return;
  }

  state.selectedMergeIds = [];
  mergeStatus.textContent = "Events merged.";
  await loadReports();
}

map.on("click", (event) => choosePin(event.latlng));

document.querySelector("#locateButton").addEventListener("click", () => {
  if (!navigator.geolocation) {
    locationStatus.textContent = "This browser does not support geolocation.";
    return;
  }

  locationStatus.textContent = "Finding your current location...";
  navigator.geolocation.getCurrentPosition(
    (position) => {
      const latlng = [position.coords.latitude, position.coords.longitude];
      map.setView(latlng, 17);
      choosePin({ lat: latlng[0], lng: latlng[1] });

      if (locationMarker) {
        locationMarker.setLatLng(latlng);
      } else {
        locationMarker = L.marker(latlng).addTo(map);
      }
      locationMarker.bindPopup("Your current location").openPopup();
      locationStatus.textContent = `Location found with ${Math.round(position.coords.accuracy)}m accuracy.`;
    },
    () => {
      locationStatus.textContent = "Location permission was denied or unavailable.";
    },
    { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 },
  );
});

windowSelect.addEventListener("change", async () => {
  state.window = windowSelect.value;
  await loadReports();
});

categoryFilter.addEventListener("change", async () => {
  state.category = categoryFilter.value;
  await loadReports();
});

searchInput.addEventListener("input", () => {
  state.search = searchInput.value;
  window.clearTimeout(searchDebounceHandle);
  searchDebounceHandle = window.setTimeout(() => {
    loadReports();
  }, 220);
});

summaryButton.addEventListener("click", refreshCampusSummary);

imageInput.addEventListener("change", async () => {
  const [file] = imageInput.files;
  if (!file) {
    clearImageSelection();
    return;
  }

  if (file.size > 1_500_000) {
    pinStatus.textContent = "Please pick an image smaller than 1.5MB.";
    clearImageSelection();
    return;
  }

  try {
    state.pendingImageData = await readFileAsDataUrl(file);
    showImagePreview(state.pendingImageData);
    pinStatus.textContent = "Picture attached to your next pin.";
  } catch (error) {
    pinStatus.textContent = error.message;
    clearImageSelection();
  }
});

clearImageButton.addEventListener("click", () => {
  clearImageSelection();
  pinStatus.textContent = "Picture removed.";
});

closePinPanel.addEventListener("click", () => {
  closePinComposer();
});

clearMergeButton.addEventListener("click", () => {
  state.selectedMergeIds = [];
  renderReports();
});

mergeButton.addEventListener("click", mergeSelectedReports);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!form.lat.value || !form.lng.value) {
    pinStatus.textContent = "Click the map or use your location before submitting.";
    return;
  }

  const payload = Object.fromEntries(new FormData(form).entries());
  payload.image_data = state.pendingImageData;

  const response = await fetch("/api/reports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    pinStatus.textContent = await response.text();
    return;
  }

  form.reset();
  clearImageSelection();
  pinStatus.textContent = "Event pinned.";
  if (selectedMarker) {
    selectedMarker.remove();
    selectedMarker = null;
  }
  closePinComposer();
  await loadReports();
});

updateActiveFilterText();
updateMergeControls();
loadReports();
