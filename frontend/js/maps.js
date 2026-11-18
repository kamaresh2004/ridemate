/* Leaflet map helpers + geocoding autocomplete (OSM / OSRM via backend). */

import { Api } from './api.js';
import { h, debounce, icon } from './ui.js';

/* Every mounted autocomplete registers here so a single document-level
   listener can close them all. Previously each attach() added its own listener
   that was never removed, leaking a detached input on every SPA navigation. */
const acInstances = new Set();
let acListSeq = 0;

document.addEventListener('click', (event) => {
  for (const ac of Array.from(acInstances)) {
    if (!ac.input.isConnected) { acInstances.delete(ac); continue; }
    if (!ac.wrap.contains(event.target)) ac.dismiss();
  }
});


function L() {
  return window.L || null;
}

/* ---------- map lifecycle ---------- */
export function createMap(el, { zoom = true } = {}) {
  if (!L() || !el) return null;
  el.innerHTML = '';
  const map = L().map(el, { zoomControl: false, attributionControl: true });
  L().tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  map.setView([12.9716, 77.5946], 11);
  if (zoom) L().control.zoom({ position: 'bottomright' }).addTo(map);
  return map;
}

export function destroyMap(map) {
  if (map) map.remove();
}

/* ---------- markers ---------- */
function pinHtml(color) {
  return `<span style="display:block;width:26px;height:26px;border-radius:50% 50% 50% 4px;transform:rotate(-45deg);background:${color};border:3px solid #fff;box-shadow:0 6px 16px rgba(16,19,38,.28)"><i style="display:block;width:9px;height:9px;border-radius:50%;background:#fff;position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);"></i></span>`;
}

export function addMarker(map, pt, { color = '#635bff', label = '' } = {}) {
  if (!map || !pt) return null;
  const marker = L().marker([pt.lat, pt.lng], {
    icon: L().divIcon({
      className: '', html: pinHtml(color),
      iconSize: [26, 26], iconAnchor: [13, 13],
    }),
  }).addTo(map);
  if (label) marker.bindTooltip(label, { direction: 'top', offset: [0, -14] });
  return marker;
}

export function fitPoints(map, points) {
  if (!map || !points.length) return;
  if (points.length === 1) { map.setView([points[0].lat, points[0].lng], 13); return; }
  map.fitBounds(L().latLngBounds(points.map((p) => [p.lat, p.lng])), { padding: [40, 40] });
}

/* ---------- routing ---------- */
export async function drawRoute(map, a, b) {
  if (!map || !a || !b) return null;
  try {
    const data = await Api.geoRoute([[a.lat, a.lng], [b.lat, b.lng]]);
    const coords = data.route.map((c) => ({ lat: c[0], lng: c[1] }));
    const line = L().polyline(coords, { color: '#635bff', weight: 4.5, opacity: 0.92 }).addTo(map);
    fitPoints(map, coords);
    line.bringToFront();
    return data;
  } catch {
    fitPoints(map, [a, b]);
    return null;
  }
}

/* ---------- autocomplete ---------- */
export function acPoint(input) {
  if (!input || !input.dataset.lat) return null;
  const lat = Number(input.dataset.lat);
  const lng = Number(input.dataset.lng);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  return {
    label: input.dataset.label || input.value,
    address: input.dataset.address || input.dataset.label,
    lat,
    lng,
  };
}

export function setAcPoint(input, pt) {
  if (!input || !pt) return false;
  const { lat, lng } = pt;
  if (lat === undefined || lat === null || lat === '' ||
      lng === undefined || lng === null || lng === '') return false;
  const nLat = Number(lat);
  const nLng = Number(lng);
  if (!Number.isFinite(nLat) || !Number.isFinite(nLng)) return false;
  // Only touch the visible text once the coordinates are known-good, so a
  // malformed suggestion can never leave a filled-looking field with no point.
  input.value = pt.label || '';
  Object.assign(input.dataset, {
    label: pt.label || '',
    address: pt.address || pt.label || '',
    lat: nLat,
    lng: nLng,
  });
  return true;
}

export function clearAcPoint(input) {
  if (!input) return;
  delete input.dataset.label;
  delete input.dataset.address;
  delete input.dataset.lat;
  delete input.dataset.lng;
}

/**
 * Attach a geocoding dropdown to a text input.
 * Uses whatever wrapper with class .ac (auto-created if missing).
 * Returns an object with set(value), clear(), point() and close().
 */
export function attachAutocomplete(input, { onPick, min = 2 } = {}) {
  if (!input) return null;
  let wrap = input.closest('.ac');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.className = 'ac ac--auto';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
  }

  const listId = `ac-list-${++acListSeq}`;
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-autocomplete', 'list');
  input.setAttribute('aria-expanded', 'false');
  input.setAttribute('aria-controls', listId);

  let list = null;
  let itemsCache = [];
  let active = -1;
  // Every query takes a ticket. Bumping the counter invalidates whatever is
  // in flight, so a slow response can never re-open the dropdown once the
  // user has picked a suggestion (or moved on). Without this the list kept
  // reappearing right after a location was selected.
  let ticket = 0;

  const close = () => {
    if (list) { list.remove(); list = null; }
    active = -1;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  };

  // Drop the list, the pending debounced query and any in-flight response.
  const dismiss = () => {
    ticket += 1;
    if (runQuery.cancel) runQuery.cancel();
    close();
  };

  const pick = (item) => {
    if (!item) return;
    dismiss();
    if (!setAcPoint(input, item)) return;
    itemsCache = [];
    input.focus();
    if (onPick) onPick(item);
  };

  const mountList = () => {
    close();
    list = document.createElement('div');
    list.className = 'ac-list';
    list.id = listId;
    wrap.appendChild(list);
    input.setAttribute('aria-expanded', 'true');
    return list;
  };

  // Inline status/error rows so a slow or failing geocoder never looks like
  // "nothing happened".
  const showState = (text, { error = false, retry = null } = {}) => {
    const el = mountList();
    el.setAttribute('aria-live', 'polite');
    const row = document.createElement('div');
    row.className = 'ac-state' + (error ? ' ac-state--error' : '');
    row.textContent = text;
    el.appendChild(row);
    if (retry) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ac-retry';
      btn.textContent = 'Try again';
      btn.addEventListener('mousedown', (e) => e.preventDefault());
      btn.addEventListener('click', retry);
      el.appendChild(btn);
    }
  };

  const setActive = (index) => {
    if (!list) return;
    const options = [...list.querySelectorAll('.ac-item')];
    if (!options.length) return;
    active = index;
    options.forEach((el, i) => {
      const on = i === active;
      el.classList.toggle('sel', on);
      el.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    const chosen = options[active];
    input.setAttribute('aria-activedescendant', chosen.id);
    chosen.scrollIntoView({ block: 'nearest' });
  };

  const renderItems = (items, { relaxedFor = null } = {}) => {
    const el = mountList();
    el.setAttribute('role', 'listbox');
    const note = relaxedFor
      ? `<div class="ac-note">Showing matches for “${h(relaxedFor)}”</div>`
      : '';
    el.innerHTML = note + items.map((it, i) => `
      <div class="ac-item" role="option" aria-selected="false" id="${listId}-${i}" data-i="${i}">
        ${icon('map-pin', 17)}
        <div><b>${h(it.label)}</b>${it.address && it.address !== it.label ? `<span>${h(it.address)}</span>` : ''}</div>
      </div>`).join('');
    el.addEventListener('mousedown', (e) => {
      const itemEl = e.target.closest('.ac-item');
      if (itemEl) {
        e.preventDefault(); // keep focus in input
        pick(items[Number(itemEl.dataset.i)]);
      }
    });
  };

  const runQuery = debounce(async (q) => {
    if (q.length < min) { dismiss(); return; }
    // The text changed (e.g. a place was picked) before this ran: skip it.
    if (input.value.trim() !== q) return;
    const mine = ++ticket;
    showState('Searching places…');
    let items = null;
    let relaxedFor = null;
    try {
      // Ask for a few extra rows: the list is clipped to the dropdown height
      // and Indian areas often share a name.
      const res = await Api.geoSearch(q, 8);
      items = Array.isArray(res.data) ? res.data : [];
      // The server widens a query that would otherwise match nothing
      // ("Thanjavur, new busstand" -> "Thanjavur"). Say so, rather than
      // pretending the dropped words were understood.
      if (items.length && res.relaxed) relaxedFor = res.query || null;
    } catch { items = null; }
    // A newer keystroke, a pick, or a click outside happened while this was
    // in flight: this answer is stale, so throw it away.
    if (mine !== ticket) return;
    if (document.activeElement !== input) { close(); return; }
    if (items === null) {
      showState("Couldn't reach place search.", { error: true, retry: () => runQuery(q) });
      return;
    }
    itemsCache = items;
    if (!items.length) {
      showState(`No places found for “${q}”. Try the town or city name.`);
      return;
    }
    renderItems(items, { relaxedFor });
  }, 380);

  const onKey = (e) => {
    const options = list ? list.querySelectorAll('.ac-item') : [];
    if (e.key === 'Escape') {
      if (list) { e.preventDefault(); dismiss(); }
      return;
    }
    if (e.key === 'Tab') { dismiss(); return; }
    if (!options.length) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const n = options.length;
      if (active < 0) setActive(e.key === 'ArrowDown' ? 0 : n - 1);
      else setActive((active + (e.key === 'ArrowDown' ? 1 : -1) + n) % n);
      return;
    }
    if (e.key === 'Enter') {
      // pick the highlighted suggestion (or the first); otherwise submit
      e.preventDefault();
      pick(itemsCache[active >= 0 ? active : 0]);
    }
  };

  input.addEventListener('input', () => {
    const value = input.value.trim();
    // Whitespace-only edits around an existing pick must not silently drop
    // the chosen coordinates (that used to block publishing).
    if (input.dataset.label && value === input.dataset.label) return;
    // Typing invalidates the previous choice; hide the old suggestions right
    // away so a stale item can't be clicked while the new query runs.
    ticket += 1;
    close();
    itemsCache = [];
    clearAcPoint(input);
    runQuery(value);
  });
  input.addEventListener('keydown', onKey);

  acInstances.add({ input, wrap, dismiss });

  return {
    set(point) { dismiss(); setAcPoint(input, point); },
    clear() { dismiss(); },
    point() { return acPoint(input); },
    close() { dismiss(); },
  };
}

/* swap origin/destination values between two autocomplete inputs */
export function swapFields(a, b) {
  const va = a.value, da = { ...a.dataset };
  const vb = b.value, db = { ...b.dataset };
  a.value = vb; Object.keys(da).forEach((k) => delete a.dataset[k]); Object.assign(a.dataset, db);
  b.value = va; Object.keys(db).forEach((k) => delete b.dataset[k]); Object.assign(b.dataset, da);
}