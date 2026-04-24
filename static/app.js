const mapEl = document.querySelector("#map");
const center = [Number(mapEl.dataset.lat), Number(mapEl.dataset.lng)];
const map = L.map("map", { zoomControl: true }).setView(center, 16);
const form = document.querySelector("#reportForm");
const pinStatus = document.querySelector("#pinStatus");
const locationStatus = document.querySelector("#locationStatus");
const feedItems = document.querySelector("#feedItems");

let selectedMarker = null;
let locationMarker = null;
let reportLayer = L.layerGroup().addTo(map);

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

function choosePin(latlng) {
  form.lat.value = latlng.lat.toFixed(6);
  form.lng.value = latlng.lng.toFixed(6);
  pinStatus.textContent = `Selected ${form.lat.value}, ${form.lng.value}`;

  if (selectedMarker) {
    selectedMarker.setLatLng(latlng);
  } else {
    selectedMarker = L.marker(latlng, { icon: selectedIcon }).addTo(map);
  }
}

function severityColor(severity) {
  if (severity === "High") return "#c8102e";
  if (severity === "Medium") return "#b77900";
  return "#1d7f63";
}

async function loadReports() {
  const response = await fetch("/api/reports");
  const reports = await response.json();
  reportLayer.clearLayers();
  feedItems.innerHTML = "";

  for (const report of reports) {
    const marker = L.circleMarker([report.lat, report.lng], {
      radius: 9,
      color: "#171717",
      weight: 2,
      fillColor: severityColor(report.severity),
      fillOpacity: 0.9,
    }).addTo(reportLayer);

    marker.bindPopup(`
      <strong>${escapeHtml(report.title)}</strong><br>
      ${escapeHtml(report.category)} · ${escapeHtml(report.severity)}<br>
      ${escapeHtml(report.description)}<br>
      <small>${escapeHtml(report.author_name)} · ${formatTime(report.created_at)}</small>
    `);

    const card = document.createElement("article");
    card.className = "feed-card";
    card.innerHTML = `
      <h3>${escapeHtml(report.title)}</h3>
      <p>${escapeHtml(report.description)}</p>
      <div class="feed-meta">
        <span>${escapeHtml(report.category)}</span>
        <span>${escapeHtml(report.severity)}</span>
        <span>${formatTime(report.created_at)}</span>
      </div>
    `;
    card.addEventListener("click", () => {
      map.setView([report.lat, report.lng], 18);
      marker.openPopup();
    });
    feedItems.appendChild(card);
  }
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

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!form.lat.value || !form.lng.value) {
    pinStatus.textContent = "Click the map or use your location before submitting.";
    return;
  }

  const payload = Object.fromEntries(new FormData(form).entries());
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
  pinStatus.textContent = "Report pinned.";
  if (selectedMarker) {
    selectedMarker.remove();
    selectedMarker = null;
  }
  await loadReports();
});

loadReports();
