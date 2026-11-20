/* Manage vehicles: grid, add/edit modal, documents, delete. */

import { Api } from './api.js';
import {
  icon, h, toast, errMsg, skeleton, emptyState, vehicleLabel, vehicleIconName,
  openModal, confirmDialog, assetUrl,
} from './ui.js';

const MAX_SEATS = { '2-wheeler': 1, '4-wheeler': 7, auto: 8, bus: 24 };
const DEFAULT_SEATS = { '2-wheeler': 1, '4-wheeler': 4, auto: 2, bus: 12 };
const MAX_DOCUMENT_BYTES = 5 * 1024 * 1024;
/* Hyphens are accepted as well as spaces because people copy the plate and DL
   straight off the paper; the server normalises both before validating. */
const REG_RE = /^([A-Z]{2}[ -]?\d{1,2}[ -]?[A-Z]{1,3}[ -]?\d{4}|\d{2}[ -]?BH[ -]?\d{4}[ -]?[A-Z]{2})$/;
const DL_RE = /^[A-Z]{2}[ -]?\d{2}[ -]?(19|20)\d{2}[ -]?\d{7}$/;
const POLICY_RE = /^[A-Za-z0-9/-]{5,40}$/;

/* Driving licences are only issued to adults; backend/identity.py enforces the
   same rule, so this is purely the inline hint. */
function adultCutoff() {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 18);
  return d.toISOString().slice(0, 10);
}

/* Captain identity KYC state, kept live while this page (or the add-vehicle
   modal) is open. The review is a human decision -- there is no self-approval
   path in the API -- so the UI only ever mirrors what the server says. */
const KYC_POLL_MS = 4000;
const KYC_STATUS = {
  verified: { label: 'Verified', cls: 'badge--success', ic: 'check' },
  submitted: { label: 'Under review', cls: 'badge--brand', ic: 'clock' },
  rejected: { label: 'Rejected', cls: 'badge--danger', ic: 'x' },
  unverified: { label: 'Not verified', cls: 'badge--warn', ic: 'alert' },
};
const KYC_DOC_LABELS = {
  dl: 'Driving licence', aadhaar: 'Aadhaar', passport: 'Passport', voter_id: 'Voter ID',
};
const PRECHECK_REASONS = {
  image_blurry: 'Photo is blurry, retake it',
  licence_no_not_found: 'Could not read the licence number',
  licence_no_mismatch: 'Number in photo differs from what you typed',
  expiry_not_found: 'Could not read the expiry date',
  expired: 'This document has expired',
  name_mismatch: 'Name does not match your profile',
  bad_file_type: 'Choose a JPG or PNG image',
  file_over_5mb: 'File is over 5 MB',
  unreadable: 'Photo could not be checked',
};

export async function renderVehicles(root) {
  root.innerHTML = `
    <div class="page-head"><div><div class="eyebrow">Ride ready</div><h1>My vehicles</h1>
    <p>Add once, reuse for every ride you publish.</p></div>
    <button class="btn btn-primary btn-lg" data-add>${icon('plus', 17)} Add vehicle</button></div>
    <section class="card card--pad mt-16" id="captainStrip" aria-live="polite">
      <div class="flex items-center justify-between gap-12" style="flex-wrap:wrap">
        <div><div class="section-title" style="margin:0">${icon('shield', 16)} Captain KYC</div>
          <p class="text-faint text-sm" style="margin-top:4px">Your identity is checked by RideMate's Trust &amp; Safety team (an admin). This panel refreshes live.</p></div>
        <span class="badge badge--ghost" id="captainBadge">${icon('clock', 13)} loading…</span>
      </div>
      <div id="captainBody" class="mt-12"></div>
    </section>
    <div class="vehicles-grid" id="vhGrid">${skeleton(2)}</div>`;

  const grid = root.querySelector('#vhGrid');
  root.querySelector('[data-add]').addEventListener('click', () => vehicleFormModal(null, refresh));

  /* Live captain state above the list. It is deliberately separate from the
     vehicle cards: identity verification and vehicle verification are two
     independent gates, and a driver needs to see which one is holding them up. */
  const strip = root.querySelector('#captainStrip');
  const stopPageKyc = pollCaptainKyc(
    (data, err) => renderCaptainStrip(strip, data, err),
    () => Boolean(strip && strip.isConnected),
  );
  const stopOnLeave = () => stopPageKyc();
  window.addEventListener('hashchange', stopOnLeave, { once: true });
  window.addEventListener('ridemate:logout', stopOnLeave, { once: true });

  async function refresh() {
    grid.innerHTML = skeleton(2);
    try {
      const res = await Api.vehicles();
      if (!res.data.length) {
        grid.innerHTML = emptyState({
          ic: 'car',
          title: 'No vehicles yet',
          desc: 'Add your car, bike or auto to start publishing rides.',
          action: '<button class="btn btn-primary btn-sm mt-12" data-add2>Add your first vehicle</button>',
        });
        grid.querySelector('[data-add2]')?.addEventListener('click', () => vehicleFormModal(null, refresh));
        return;
      }
      grid.innerHTML = res.data.map(card).join('');
      grid.querySelectorAll('[data-edit]').forEach((el) => {
        el.addEventListener('click', () => {
          const v = res.data.find((x) => x.id === el.dataset.edit);
          vehicleFormModal(v, refresh);
        });
      });
      grid.querySelectorAll('[data-del]').forEach((el) => {
        el.addEventListener('click', () => deleteVehicle(el.dataset.del, refresh));
      });
      grid.querySelectorAll('[data-submit-kyc]').forEach((el) => {
        el.addEventListener('click', async () => {
          el.disabled = true;
          try {
            await Api.submitVehicleVerification(el.dataset.submitKyc);
            toast('Documents submitted for manual review.', 'success');
            await refresh();
          } catch (err) {
            toast(errMsg(err), 'error');
            el.disabled = false;
          }
        });
      });
      wireDocs(grid, res.data);
    } catch (err) {
      grid.innerHTML = emptyState({ ic: 'alert', title: 'Could not load vehicles', desc: errMsg(err) });
    }
  }

  refresh();
}

/* ------------------------------------------------------------------ captain KYC
   Poll /api/kyc/me while a screen is open and stop as soon as its container is
   gone. Polling (rather than SSE) is the pattern the Captain page already uses:
   the access token is memory-only, and EventSource cannot send an
   Authorization header. */
function pollCaptainKyc(onData, isAlive = () => true) {
  let timer = null;
  let stopped = false;
  const stop = () => {
    stopped = true;
    if (timer) clearInterval(timer);
    timer = null;
  };
  const tick = async () => {
    if (stopped || !isAlive()) { stop(); return; }
    try {
      const data = await Api.myKyc();
      if (stopped || !isAlive()) { stop(); return; }
      onData(data);
    } catch (err) {
      if (stopped || !isAlive()) { stop(); return; }
      onData(null, err);
    }
  };
  tick();
  timer = setInterval(tick, KYC_POLL_MS);
  return stop;
}

function kycBadge(state) {
  const meta = KYC_STATUS[state] || KYC_STATUS.unverified;
  return `<span class="badge ${meta.cls}">${icon(meta.ic, 13)} ${meta.label}</span>`;
}

function reviewerOf(data) {
  return data?.review?.reviewer || "RideMate Trust & Safety team";
}

function kycTime(value) {
  if (!value) return '';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

function renderCaptainStrip(el, data, err) {
  if (!el) return;
  const badge = el.querySelector('#captainBadge');
  const body = el.querySelector('#captainBody');
  if (!badge || !body) return;

  if (err) {
    badge.className = 'badge badge--danger';
    badge.innerHTML = `${icon('alert', 13)} offline`;
    body.innerHTML = `<p class="text-sm">Live status is unavailable right now. ${h(errMsg(err))}</p>`;
    return;
  }

  const kyc = data?.kyc || {};
  const state = kyc.kyc_status || 'unverified';
  const meta = KYC_STATUS[state] || KYC_STATUS.unverified;
  badge.className = `badge ${meta.cls}`;
  badge.innerHTML = `${icon(meta.ic, 13)} ${meta.label}`;

  const reviewer = h(reviewerOf(data));
  const updated = kycTime(kyc.reviewed_at || kyc.submitted_at);
  const cta = '<a class="btn btn-outline btn-sm" href="#/driver-kyc">Open Captain verification</a>';

  if (state === 'verified') {
    body.innerHTML = `<p class="text-sm">${icon('check', 14)} Identity verified by ${reviewer}${updated ? ` · ${h(updated)}` : ''}. A vehicle still needs its own review before publishing.</p>`;
  } else if (state === 'submitted') {
    body.innerHTML = `<p class="text-sm">${icon('clock', 14)} Your identity document is with ${reviewer}. This panel updates automatically when a decision is recorded.</p>`;
  } else if (state === 'rejected') {
    body.innerHTML = `<p class="text-sm" style="color:var(--red)">${icon('x', 14)} ${h(kyc.reason || 'Your identity document was not accepted.')}</p>
      <div class="mt-8">${cta}</div>`;
  } else {
    body.innerHTML = `<p class="text-sm">${icon('alert', 14)} Your identity has not been verified yet. Publishing needs both an approved identity and an approved vehicle.</p>
      <div class="mt-8">${cta}</div>`;
  }
}

function renderModalCaptainKyc(el, data, err) {
  if (!el) return;
  const head = el.querySelector('[data-kyc-head]');
  const body = el.querySelector('[data-kyc-body]');
  if (!head || !body) return;

  if (err) {
    head.innerHTML = `<span class="badge badge--danger">${icon('alert', 13)} status unavailable</span>`;
    body.innerHTML = `<p class="text-faint text-sm">${h(errMsg(err))}</p>`;
    return;
  }

  const kyc = data?.kyc || {};
  const state = kyc.kyc_status || 'unverified';
  head.innerHTML = kycBadge(state);

  // A poll must never wipe a document number the captain is typing.
  if (body.contains(document.activeElement) && body.querySelector('[data-kyc-form]')) return;

  const reviewer = h(reviewerOf(data));
  const reviewed = kycTime(kyc.reviewed_at || kyc.submitted_at);

  if (state === 'verified') {
    body.innerHTML = `<p class="text-sm" style="color:var(--green)">${icon('check', 14)} Identity verified by ${reviewer}${reviewed ? ` · ${h(reviewed)}` : ''}. You can submit this vehicle for review.</p>`;
    return;
  }
  if (state === 'submitted') {
    body.innerHTML = `<p class="text-sm">${icon('clock', 14)} Your identity document is with ${reviewer}. This panel refreshes every few seconds and will switch to verified automatically.</p>`;
    return;
  }

  const types = data?.allowed_document_types?.length ? data.allowed_document_types : ['dl', 'aadhaar'];
  const reason = state === 'rejected' && kyc.reason
    ? `<p class="text-sm mt-8" style="color:var(--red)">${icon('x', 14)} ${h(kyc.reason)}</p>`
    : '';
  body.innerHTML = `
    ${reason}
    <p class="text-sm">Publishing needs an approved identity <b>and</b> an approved vehicle. Upload your identity now — a ${reviewer} reviewer checks it, and this panel updates live.</p>
    <div class="mt-12" data-kyc-form>
      <div class="grid-2">
        <div class="field"><label>Document type</label>
          <select class="select" data-kyc-doc-type>
            ${types.map((t) => `<option value="${h(t)}">${h(KYC_DOC_LABELS[t] || t)}</option>`).join('')}
          </select></div>
        <div class="field"><label>Document number</label>
          <input class="input" data-kyc-number required minlength="4" maxlength="32" placeholder="KA0519930123456" /></div>
      </div>
      <div class="grid-2">
        <div class="field"><label>Full name (as printed)</label>
          <input class="input" data-kyc-name maxlength="120" placeholder="Name on the document" /></div>
        <div class="field"><label>Date of birth</label>
          <input class="input" type="date" data-kyc-dob max="${adultCutoff()}" required /></div>
      </div>
      <div class="field"><label>Photo or PDF of the document</label>
        <input class="input" type="file" data-kyc-file accept=".pdf,.png,.jpg,.jpeg,.webp" required /></div>
      <button type="button" class="btn btn-primary btn-sm mt-8" data-kyc-submit>${icon('shield', 14)} Submit identity for review</button>
      <p class="text-faint text-xs mt-8">Only the last four digits are stored. A person, not an automated score, makes the final decision.</p>
    </div>`;

  body.querySelector('[data-kyc-submit]').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const number = body.querySelector('[data-kyc-number]').value.trim();
    const file = body.querySelector('[data-kyc-file]').files?.[0];
    const dob = body.querySelector('[data-kyc-dob]').value;
    if (!number) { toast('Enter the document number.', 'error'); return; }
    if (!file) { toast('Attach a photo or PDF of the document.', 'error'); return; }
    if (!dob) { toast('Enter your date of birth.', 'error'); return; }
    if (dob > adultCutoff()) {
      toast('You must be at least 18 years old to verify a driving licence.', 'error');
      return;
    }
    button.disabled = true;
    button.textContent = 'Submitting…';
    try {
      await Api.submitIdentity({
        doc_type: body.querySelector('[data-kyc-doc-type]').value,
        doc_number: number,
        full_name: body.querySelector('[data-kyc-name]').value.trim(),
        date_of_birth: body.querySelector('[data-kyc-dob]').value,
      }, file);
      toast('Identity submitted — a RideMate reviewer will check it.', 'success');
      renderModalCaptainKyc(el, await Api.myKyc());
    } catch (err) {
      toast(errMsg(err), 'error');
      button.disabled = false;
      button.innerHTML = `${icon('shield', 14)} Submit identity for review`;
    }
  });
}

function applyServerFieldErrors(form, err) {
  const fields = err?.details?.fields || [];
  if (!fields.length) return false;
  fields.forEach((name) => {
    const input = form.querySelector(`[name="${name}"]`);
    if (!input) return;
    input.setCustomValidity(err.message || 'Please check this field.');
    input.reportValidity();
    input.addEventListener('input', () => input.setCustomValidity(''), { once: true });
  });
  return true;
}

function card(v) {
  const kyc = v.kyc || {};
  const state = v.verification_status || 'unverified';
  const meta = KYC_STATUS[state] || KYC_STATUS.unverified;
  /* Only a packet still on the captain's side has gaps to close. A verified
     vehicle keeps its decision even if legacy seed data has no documents. */
  const actionable = state === 'unverified' || state === 'rejected';
  const missing = actionable ? (kyc.missing_labels || kyc.missing || []) : [];
  /* A vehicle that cannot be submitted used to show no action at all: the
     captain saw uploaded papers, no "Submit" button and no explanation, and
     only found out at publish time. Name the gap and offer the fix here. */
  const needs = missing.length
    ? `<p class="text-sm mt-8" style="color:color-mix(in srgb, var(--amber) 80%, #7a4b00)">${icon('alert', 14)} Still needed: <b>${h(missing.join(', '))}</b></p>`
    : '';
  const cta = kyc.can_submit
    ? `<button class="btn btn-outline btn-sm" data-submit-kyc="${h(v.id)}">${icon('shield', 14)} Submit for review</button>`
    : missing.length
      ? `<button class="btn btn-outline btn-sm" data-edit="${h(v.id)}">${icon('edit', 14)} Add details</button>`
      : '';
  const hint = state === 'verified'
    ? 'Approved — this vehicle can publish rides.'
    : state === 'submitted'
      ? 'An admin is checking these papers. Publishing unlocks when the review is approved.'
      : state === 'rejected'
        ? 'Fix the papers, then resubmit for review.'
        : missing.length
          ? 'Fill the gaps, then submit for review.'
          : 'Ready to send to an admin for review.';

  return `
  <article class="card card--pad card--hover vehicle-card" data-id="${v.id}">
    <div class="flex items-center justify-between mb-12">
      <span class="vehicle-ico">${icon(vehicleIconName(v.vehicle_type), 22)}</span>
      <div class="flex gap-4">
        <button class="btn-icon btn-icon--ghost" data-edit="${v.id}" title="Edit" aria-label="Edit">${icon('edit', 16)}</button>
        <button class="btn-icon btn-icon--ghost" data-del="${v.id}" title="Delete" aria-label="Delete" style="color:var(--red)">${icon('trash', 16)}</button>
      </div>
    </div>
    <b class="text-sm" style="font-size:15px">${h(v.vehicle_model || 'Unnamed vehicle')}</b>
    <div class="text-faint text-sm" style="margin-top:2px">${h(v.vehicle_number)}</div>
    <div class="flex gap-8 items-center mt-12" style="flex-wrap:wrap">
      <span class="badge badge--brand">${vehicleLabel(v.vehicle_type)}</span>
      <span class="badge badge--ghost">${icon('users', 13)} ${v.seat_count} seats</span>
      ${v.color ? `<span class="badge badge--ghost">${h(v.color)}</span>` : ''}
    </div>
    <div class="mt-16" style="border-top:1px dashed var(--line);padding-top:12px">
      <div class="flex items-center justify-between" style="font-size:12.5px">
        <span class="text-faint">Driving licence</span>
        ${docSlot('dl', v, 'licence')}
      </div>
      <div class="flex items-center justify-between" style="font-size:12.5px;margin-top:8px">
        <span class="text-faint">Registration certificate (RC)</span>
        <span class="badge ${v.rc?.rc_status === 'approved' ? 'badge--success' : 'badge--ghost'}">${h(v.rc?.rc_status || 'not submitted')}</span>
      </div>
      ${v.rc?.has_document ? '<button type="button" class="badge badge--success mt-8" data-view-rc="' + h(v.id) + '">View submitted RC</button>' : ''}
      ${v.rc?.rc_number_masked ? '<p class="text-faint text-xs mt-8">RC number: ' + h(v.rc.rc_number_masked) + '</p>' : ''}
      ${v.rc?.reason ? '<p class="text-faint text-sm mt-8">RC review note: ' + h(v.rc.reason) + '</p>' : ''}
      ${v.rc?.can_submit ? '<div class="flex gap-8 mt-8" style="align-items:center"><input class="input" data-rc-number="' + h(v.id) + '" placeholder="RC number" aria-label="RC number" style="max-width:180px"><label class="badge badge--ghost" style="cursor:pointer">Upload RC<input type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" hidden data-rc-doc="' + h(v.id) + '"></label></div>' : ''}
      <div class="flex items-center justify-between" style="font-size:12.5px;margin-top:8px">
        <span class="text-faint">Insurance</span>
        ${docSlot('insurance', v, 'insurance')}
      </div>
      <div class="flex items-center justify-between gap-8 mt-12" style="font-size:12.5px;flex-wrap:wrap">
        <span class="badge ${meta.cls}">${icon(meta.ic, 13)} ${meta.label}</span>
        ${cta}
      </div>
      ${needs}
      <p class="text-faint text-xs mt-8">${h(hint)} Papers are reviewed by RideMate's Trust &amp; Safety team (admin). Your captain identity is a separate check.</p>
      ${state === 'rejected' && kyc.reason ? '<p class="text-faint text-sm mt-8">Review note: ' + h(kyc.reason) + '</p>' : ''}
    </div>
  </article>`;
}

function docSlot(kind, v, label) {
  const url = v[`${kind}_document_url`];
  // A stored file is not an approved file: DL and insurance are decided when the
  // whole packet is reviewed, so the badge stays neutral instead of claiming a
  // green "verified" that the server never granted.
  return `<span class="flex gap-4 items-center">
    ${url
      ? `<button type="button" class="badge badge--ghost" data-view-doc data-url="${h(url)}" title="View uploaded ${label}">${icon('eye', 12)} ${label} uploaded</button>`
      : ''}
    <label class="badge badge--ghost" style="cursor:pointer" title="${url ? 'Replace' : 'Upload'} ${label}">${icon('upload', 12)} ${url ? 'replace' : 'add'}
      <input type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" hidden data-doc="${kind}" data-vid="${v.id}" /></label>
  </span>`;
}

function docError(err) {
  // A document whose bytes are gone answers 404 even though its metadata still
  // says "uploaded". Say what to do instead of surfacing "File not found."
  if (err && (err.status === 404 || err.code === 'not_found')) {
    return 'This file is no longer on the server — use “replace” to upload it again.';
  }
  return errMsg(err);
}

function wireDocs(grid, vehicles) {
  grid.querySelectorAll('[data-view-rc]').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const blob = await Api.downloadVehicleDoc('/api/kyc/vehicles/' + button.dataset.viewRc + '/rc/document');
        const tab = window.open(URL.createObjectURL(blob), '_blank');
        if (tab) tab.opener = null;
        else toast('Your browser blocked the document tab. Allow pop-ups and try again.', 'error');
      } catch (err) { toast(docError(err), 'error'); }
      finally { button.disabled = false; }
    });
  });
  grid.querySelectorAll('[data-rc-doc]').forEach((input) => {
    input.addEventListener('change', async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      const number = [...grid.querySelectorAll('[data-rc-number]')].find((el) => el.dataset.rcNumber === input.dataset.rcDoc)?.value?.trim();
      if (!number) { toast('Enter the RC number before uploading the document.', 'error'); return; }
      input.disabled = true;
      try {
        await Api.submitRc(input.dataset.rcDoc, number, file);
        toast('RC submitted for manual review.', 'success');
        await renderVehicles(document.getElementById('view'));
      } catch (err) { toast(errMsg(err), 'error'); input.disabled = false; }
    });
  });
  grid.querySelectorAll('[data-view-doc]').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const blob = await Api.downloadVehicleDoc(button.dataset.url);
        const tab = window.open(URL.createObjectURL(blob), '_blank');
        if (tab) tab.opener = null;
        else toast('Your browser blocked the document tab. Allow pop-ups and try again.', 'error');
      } catch (err) {
        toast(docError(err), 'error');
      } finally {
        button.disabled = false;
      }
    });
  });
  grid.querySelectorAll('[data-doc]').forEach((input) => {
    input.addEventListener('change', async () => {
      if (!input.files || !input.files[0]) return;
      try {
        await Api.uploadDoc(input.dataset.vid, input.dataset.doc, input.files[0]);
        toast('Document uploaded.', 'success');
        renderVehicles(document.getElementById('view'));
      } catch (err) {
        toast(errMsg(err), 'error');
      }
    });
  });
}

async function deleteVehicle(id, refresh) {
  const ok = await confirmDialog({
    title: 'Delete this vehicle?',
    message: 'Stop publishing rides with this vehicle? Active rides must be cancelled first.',
    confirmLabel: 'Delete', danger: true,
  });
  if (!ok) return;
  try {
    await Api.deleteVehicle(id);
    toast('Vehicle deleted.', 'success');
    refresh();
  } catch (err) {
    toast(errMsg(err), 'error');
  }
}

function vehicleFormModal(v, onSaved) {
  const isEdit = !!v;
  const modal = openModal({
    title: isEdit ? 'Edit vehicle' : 'Add a vehicle',
    body: `
      <section class="card card--pad" data-captain-kyc aria-live="polite" style="margin-bottom:16px">
        <div class="flex items-center justify-between gap-12" style="flex-wrap:wrap">
          <div>
            <div class="section-title" style="margin:0">${icon('shield', 16)} Captain KYC</div>
            <p class="text-faint text-sm" style="margin-top:4px">Your identity is reviewed by RideMate's Trust &amp; Safety team (admin). This panel stays live while you add the vehicle.</p>
          </div>
          <span data-kyc-head><span class="badge badge--ghost">${icon('clock', 13)} loading…</span></span>
        </div>
        <div class="mt-8" data-kyc-body><p class="text-faint text-sm">Checking your identity status…</p></div>
      </section>
      <form id="vhForm" novalidate>
        <div class="flex flex-col gap-12">
          <div class="grid-2">
            <div class="field"><label>Vehicle type</label>
              <select class="select" name="vehicle_type" required>
                ${['2-wheeler', '4-wheeler', 'auto', 'bus'].map((t) => `<option value="${t}" ${v && v.vehicle_type === t ? 'selected' : ''}>${vehicleLabel(t)}</option>`).join('')}
              </select></div>
            <div class="field"><label>Seat count</label>
              <input class="input" type="number" name="seat_count" min="1" max="${MAX_SEATS['4-wheeler']}" value="${v ? v.seat_count : DEFAULT_SEATS['4-wheeler']}" required /></div>
          </div>
          <div class="field"><label>Registration number</label>
            <input class="input" name="vehicle_number" value="${v ? h(v.vehicle_number) : ''}" placeholder="KA 05 MJ 4821" required style="text-transform:uppercase" /></div>
          <div class="field"><label>Model</label>
            <input class="input" name="vehicle_model" value="${v ? h(v.vehicle_model) : ''}" placeholder="e.g. Hyundai Creta" /></div>
          <div class="grid-2">
            <div class="field"><label>Driving licence no.</label>
              <input class="input" name="dl_number" value="${v ? h(v.dl_number || '') : ''}" placeholder="KA05 1993 0123456" /></div>
            <div class="field"><label>Insurance policy no.</label>
              <input class="input" name="insurance_number" value="${v ? h(v.insurance_number || '') : ''}" placeholder="Insurance policy number" />
            </div>
          </div>
          ${!isEdit ? `
          <div class="grid-2">
            <div class="field"><label>Licence photo</label>
              <input class="input" type="file" accept="image/jpeg,image/png" data-precheck="licence" />
              <small class="form-hint" data-precheck-status="licence" aria-live="polite"></small></div>
            <div class="field"><label>RC photo</label>
              <input class="input" type="file" accept="image/jpeg,image/png" data-precheck="rc" />
              <small class="form-hint" data-precheck-status="rc" aria-live="polite"></small></div>
          </div>
          <div class="field"><label>Insurance photo</label>
            <input class="input" type="file" accept="image/jpeg,image/png" data-precheck="insurance" />
            <small class="form-hint" data-precheck-status="insurance" aria-live="polite"></small></div>
          <label class="text-sm" data-kyc-consent hidden>
            <input type="checkbox" data-kyc-consent-check />
            <span data-kyc-consent-copy>I agree to document checks by RideMate's verification provider.</span>
          </label>
          ` : ''}
          <div class="field"><label>Colour <span class="text-faint">(optional)</span></label>
            <input class="input" name="color" value="${v ? h(v.color || '') : ''}" placeholder="e.g. Pearl White" /></div>
          <div class="field"><label>Notes <span class="text-faint">(optional)</span></label>
            <textarea class="textarea" name="notes" placeholder="Anything riders should know" style="min-height:60px">${v ? h(v.notes || '') : ''}</textarea></div>
        </div>
      </form>`,
    foot: `<button class="btn btn-outline" data-cancel>Cancel</button>
           <button class="btn btn-primary" data-save>${isEdit ? 'Save changes' : 'Add vehicle'}</button>`,
    closable: true,
  });

  /* Live captain identity state inside the add/edit dialog. The captain can
     submit the identity document without leaving the vehicle form, and the
     status (including an admin's later decision) appears here on its own. */
  const kycSection = modal.el.querySelector('[data-captain-kyc]');
  let captainKyc = null;
  const stopKyc = pollCaptainKyc(
    (data, err) => {
      if (data) captainKyc = data.kyc;
      renderModalCaptainKyc(kycSection, data, err);
    },
    () => Boolean(modal.el && modal.el.isConnected),
  );
  const closeModal = modal.close.bind(modal);
  modal.close = (...args) => {
    stopKyc();
    return closeModal(...args);
  };

  const save = async () => {
    const form = modal.el.querySelector('#vhForm');
    const data = Object.fromEntries(new FormData(form).entries());
    if (!form.reportValidity()) return;

    const regInput = form.querySelector('[name="vehicle_number"]');
    const dlInput = form.querySelector('[name="dl_number"]');
    const policyInput = form.querySelector('[name="insurance_number"]');
    const registration = regInput.value.trim().toUpperCase();
    const dlNumber = dlInput.value.trim().toUpperCase();
    const policy = policyInput.value.trim();
    regInput.setCustomValidity(!registration || REG_RE.test(registration)
      ? '' : 'Format like KA 05 MJ 4821');
    dlInput.setCustomValidity(!dlNumber || DL_RE.test(dlNumber)
      ? '' : 'Format like KA05 1993 0123456');
    policyInput.setCustomValidity(!policy || POLICY_RE.test(policy)
      ? '' : 'Use 5-40 letters, digits, / or -');
    if (!form.reportValidity()) return;

    const files = {
      dl: form.querySelector('[data-precheck="licence"]')?.files?.[0] || null,
      rc: form.querySelector('[data-precheck="rc"]')?.files?.[0] || null,
      insurance: form.querySelector('[data-precheck="insurance"]')?.files?.[0] || null,
    };
    for (const [kind, file] of Object.entries(files)) {
      if (!file) continue;
      if (!['image/jpeg', 'image/png'].includes(file.type)) {
        setPrecheckStatus(modal.el, kind, '⚠️ Choose a JPG or PNG image');
        return;
      }
      if (file.size > MAX_DOCUMENT_BYTES) {
        setPrecheckStatus(modal.el, kind, '⚠️ File is over 5 MB');
        return;
      }
    }
    const completePacket = Boolean(
      !isEdit && files.dl && files.insurance
      && String(data.dl_number || '').trim()
      && String(data.insurance_number || '').trim());
    if (completePacket) {
      try {
        const kyc = await Api.myKyc();
        if (kyc.consent?.required && !kyc.consent.granted) {
          const consent = modal.el.querySelector('[data-kyc-consent]');
          consent.hidden = false;
          consent.querySelector('[data-kyc-consent-copy]').textContent =
            `I agree to RideMate's verification provider checking my uploaded documents (consent text ${kyc.consent.text_version || ''}).`;
          const checkbox = consent.querySelector('[data-kyc-consent-check]');
          if (!checkbox.checked) {
            toast('Give permission for document checks before submitting this packet.', 'error');
            return;
          }
          await Api.recordKycConsent();
        }
      } catch (err) {
        toast(errMsg(err), 'error');
        return;
      }
    }

    const submit = modal.el.querySelector('[data-save]');
    submit.disabled = true;
    submit.innerHTML = `${spinnerInline()}`;
    const payload = isEdit ? changedFields(v, data) : data;
    let createdId = null;
    let submittedForReview = Boolean(completePacket);
    try {
      if (isEdit) {
        const updated = await Api.updateVehicle(v.id, payload);
        /* Adding the last missing detail (usually the insurance policy number)
           should not leave a complete packet sitting unsubmitted with the
           "Submit" button as the only way forward. The server decides whether
           the packet is complete; a refusal just means it stays on the card. */
        if (!v.kyc?.can_submit && updated?.vehicle?.kyc?.can_submit) {
          try {
            await Api.submitVehicleVerification(v.id);
            submittedForReview = true;
          } catch { /* the card still offers Submit for review */ }
        }
      } else {
        const created = await Api.createVehicle(payload);
        createdId = created.vehicle.id;
        if (files.dl) await Api.uploadDoc(createdId, 'dl', files.dl);
        if (files.insurance) await Api.uploadDoc(createdId, 'insurance', files.insurance);
        if (files.rc) await Api.submitRc(createdId, registration, files.rc);

        if (completePacket) await Api.submitVehicleVerification(createdId);
      }
      modal.close();
      const hasDocuments = Object.values(files).some(Boolean);
      const identityState = captainKyc?.kyc_status;
      const identityPending = Boolean(identityState) && identityState !== 'verified';
      const message = isEdit
        ? submittedForReview
          ? 'Vehicle updated and submitted for admin review.'
          : 'Vehicle updated.'
        : completePacket ? 'Vehicle and documents submitted for admin review.'
          : files.rc
            ? 'Vehicle saved. RC is pending admin review; complete the licence and insurance packet for vehicle review.'
            : hasDocuments
              ? 'Vehicle and documents saved. Complete the licence and insurance packet to submit for review.'
              : 'Vehicle added. You can upload its documents from My vehicles.';
      // Vehicle creation never waits on identity KYC, but a captain who has not
      // been verified yet should not think they can publish: say so once, on
      // the way out, and leave the live panel to track the decision.
      toast(identityPending
        ? `${message} Captain identity is still ${identityState === 'submitted' ? 'under review' : 'unverified'} — publishing needs an approved identity too.`
        : message, 'success');
      onSaved();
    } catch (err) {
      if (createdId) {
        modal.close();
        toast(`Vehicle was saved, but document submission did not finish: ${errMsg(err)}. You can retry from My vehicles.`, 'error');
        onSaved();
        return;
      }
      submit.disabled = false;
      submit.textContent = isEdit ? 'Save changes' : 'Add vehicle';
      if (!applyServerFieldErrors(modal.el.querySelector('#vhForm'), err)) {
        toast(errMsg(err), 'error');
      }
    }
  };

  modal.el.querySelector('[data-cancel]').addEventListener('click', () => modal.close());
  modal.el.querySelector('[data-save]').addEventListener('click', save);
  formSubmitEnter(modal.el);

  const typeSel = modal.el.querySelector('[name="vehicle_type"]');
  const seatInput = modal.el.querySelector('[name="seat_count"]');
  const regInput = modal.el.querySelector('[name="vehicle_number"]');
  const dlInput = modal.el.querySelector('[name="dl_number"]');
  bindFormat(regInput, REG_RE, 'Format like KA 05 MJ 4821');
  bindFormat(dlInput, DL_RE, 'Format like KA05 1993 0123456');
  modal.el.querySelectorAll('[data-precheck]').forEach((input) => {
    input.addEventListener('change', () => runPrecheck(input, dlInput.value));
  });
  dlInput.addEventListener('change', () => {
    const licenceInput = modal.el.querySelector('[data-precheck="licence"]');
    if (licenceInput?.files?.[0]) runPrecheck(licenceInput, dlInput.value);
  });
  const applySeatLimits = () => {
    const t = typeSel.value;
    const max = MAX_SEATS[t] || 24;
    seatInput.max = max;
    seatInput.min = 1;
    if (!isEdit) {
      seatInput.value = DEFAULT_SEATS[t] ?? 4;
      return;
    }
    const cur = Number(seatInput.value) || 1;
    if (cur > max) seatInput.value = max;
    if (cur < 1) seatInput.value = 1;
  };
  typeSel.addEventListener('change', applySeatLimits);
  applySeatLimits();

  function bindFormat(input, re, message) {
    input.addEventListener('input', () => {
      const value = input.value.trim().toUpperCase();
      input.setCustomValidity(!value || re.test(value) ? '' : message);
      input.reportValidity();
    });
  }

  async function runPrecheck(input, licenceNo) {
    const kind = input.dataset.precheck;
    const file = input.files && input.files[0];
    if (!file) return;
    if (!['image/jpeg', 'image/png'].includes(file.type)) {
      setPrecheckStatus(modal.el, kind, '⚠️ Choose a JPG or PNG image');
      return;
    }
    if (file.size > MAX_DOCUMENT_BYTES) {
      setPrecheckStatus(modal.el, kind, '⚠️ File is over 5 MB');
      return;
    }
    setPrecheckStatus(modal.el, kind, '⏳ Checking...');
    try {
      const checked = kind === 'licence'
        ? await Api.precheckLicence(file, licenceNo)
        : await Api.precheckImage(kind, file);
      if (input.files?.[0] !== file) return;
      const reasons = checked.reasons || [];
      setPrecheckStatus(modal.el, kind, reasons.length
        ? `⚠️ ${reasons.map((reason) => PRECHECK_REASONS[reason] || reason).join(', ')}`
        : kind === 'licence'
          ? '✅ Looks good, will be reviewed'
          : '✅ Image ready for manual review');
    } catch (err) {
      if (input.files?.[0] !== file) return;
      setPrecheckStatus(modal.el, kind, err.status === 413
        ? '⚠️ File is over 5 MB'
        : err.status === 400
          ? '⚠️ Choose a valid JPG or PNG image'
          : '⚠️ Check unavailable, you can still submit');
    }
  }

  function formSubmitEnter(scope) {
    scope.querySelectorAll('input').forEach((input) => {
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); save(); }
      });
    });
  }
}

function setPrecheckStatus(scope, kind, message) {
  const status = scope.querySelector(`[data-precheck-status="${kind}"]`);
  if (status) status.textContent = message;
}

function spinnerInline() {
  return '<span class="spin" style="margin-right:6px"></span>';
}

function changedFields(prev, data) {
  const out = {};
  for (const [k, cur] of Object.entries(data)) {
    const prevVal = prev[k] == null ? '' : String(prev[k]);
    const curVal = k === 'seat_count' ? String(Number(cur)) : cur;
    if (curVal !== prevVal) out[k] = k === 'seat_count' ? Number(cur) : cur;
  }
  return out;
}