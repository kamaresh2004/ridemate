/* Captain (driver) verification: identity KYC + vehicle KYC, kept live.

   Two independent gates stand between a logged-in user and taking passengers:

     1. identity  -- an approved DL / Aadhaar document on the user account
     2. vehicle   -- an approved DL + insurance packet on at least one vehicle

   Both are human decisions recorded server-side (backend/identity.py and
   backend/kyc.py); this page only ever shows status and moves documents *into*
   review. It refreshes on a short interval instead of holding an SSE connection
   because the access token is memory-only and EventSource cannot send an
   Authorization header. */

import { Api } from './api.js';
import { h, toast, errMsg, icon, emptyState, skeleton } from './ui.js';

const POLL_MS = 4000;

/* Driving licences are only issued to adults, so the form stops an under-18
   date here and the server enforces the same rule (backend/identity.py). */
function adultCutoff() {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 18);
  return d.toISOString().slice(0, 10);
}

const isAdult = (dob) => Boolean(dob) && dob <= adultCutoff();

const DOC_LABELS = {
  dl: 'Driving licence',
  aadhaar: 'Aadhaar',
  passport: 'Passport',
  voter_id: 'Voter ID',
};

const STATUS = {
  verified: { label: 'Verified', cls: 'badge--success', ic: 'check' },
  submitted: { label: 'Under review', cls: 'badge--brand', ic: 'clock' },
  rejected: { label: 'Rejected', cls: 'badge--danger', ic: 'x' },
  unverified: { label: 'Not started', cls: 'badge--ghost', ic: 'alert' },
};

/* Automated-check wording. Deliberately neutral: these are document checks
   against the paperwork supplied, not a government verification claim. */
const AUTO = {
  checking: { label: 'Checking documents…', cls: 'badge--brand' },
  pass: { label: 'Documents checked', cls: 'badge--success' },
  fail: { label: 'Check needs attention', cls: 'badge--danger' },
  inconclusive: { label: 'Review needed', cls: 'badge--warn' },
  unavailable: { label: 'Manual review', cls: 'badge--warn' },
};

const badge = (state) => {
  const meta = STATUS[state] || STATUS.unverified;
  return `<span class="badge ${meta.cls}">${icon(meta.ic, 13)} ${meta.label}</span>`;
};

export async function renderDriverKyc(root) {
  let stopped = false;
  let timer = null;
  let lastKey = '';

  root.innerHTML = `
    <div class="page-head">
      <div><div class="eyebrow">Captain verification</div><h1>Drive with RideMate</h1>
        <p>Identity and vehicle documents are reviewed by <b>RideMate's Trust &amp;
           Safety team</b> (an admin or support reviewer) &mdash; no self-approval.
           Publishing unlocks the moment both show verified; this page updates on
           its own.</p></div>
      <span class="badge badge--ghost" id="kycLive">${icon('clock', 13)} connecting…</span>
    </div>
    <section class="mt-16" id="consentSection"></section>
    <section class="mt-16" id="idSection">${skeleton(1)}</section>
    <section class="mt-16" id="vSection">${skeleton(1)}</section>`;

  const liveEl = root.querySelector('#kycLive');
  const stop = () => {
    stopped = true;
    if (timer) clearInterval(timer);
    timer = null;
  };
  window.addEventListener('hashchange', stop, { once: true });
  window.addEventListener('ridemate:logout', stop, { once: true });

  async function refresh({ quiet = false } = {}) {
    if (stopped) return;
    let data;
    try {
      const [me, vs] = await Promise.all([Api.myKyc(), Api.vehicles()]);
      data = {
        kyc: me.kyc,
        docTypes: me.allowed_document_types || [],
        vehicles: vs.data || [],
        consent: me.consent || { required: false, granted: true },
        provider: me.provider || 'manual',
        review: me.review || {},
      };
    } catch (err) {
      if (!quiet) {
        root.querySelector('#idSection').innerHTML = emptyState({
          ic: 'alert', title: 'Could not load verification status', desc: errMsg(err),
        });
        root.querySelector('#vSection').innerHTML = '';
      }
      liveEl.className = 'badge badge--danger';
      liveEl.innerHTML = `${icon('alert', 13)} offline`;
      return;
    }

    const key = [
      data.kyc.kyc_status,
      data.consent.granted ? 'consent' : 'no-consent',
      ...data.vehicles.map((v) => `${v.verification_status}:${v.kyc?.auto_check?.status || ''}`),
    ].join('|');
    if (quiet && key === lastKey) {
      stampLive(liveEl, 'live');
      return;
    }
    if (quiet && lastKey && key !== lastKey) announce(data);

    lastKey = key;
    stampLive(liveEl, 'live');
    renderConsent(root.querySelector('#consentSection'), data, refresh);
    renderIdentity(root.querySelector('#idSection'), data, refresh);
    renderVehicles(root.querySelector('#vSection'), data, refresh);
  }

  await refresh();
  timer = setInterval(() => refresh({ quiet: true }), POLL_MS);
}

function stampLive(el, mode) {
  if (!el) return;
  el.className = 'badge badge--success';
  el.innerHTML = `${icon('zap', 13)} live · ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
  el.title = mode === 'live' ? 'Status refreshes every few seconds' : '';
}

function announce(data) {
  if (data.kyc.kyc_status === 'verified') toast('Identity verified. You can drive.', 'success');
  else if (data.kyc.kyc_status === 'rejected') toast('Identity check needs another document.', 'error');
  else if (data.vehicles.some((v) => v.verification_status === 'verified')) {
    toast('Vehicle verified.', 'success');
  } else if (data.vehicles.some((v) => v.verification_status === 'rejected')) {
    toast('A vehicle review was rejected — check the note.', 'error');
  }
}

/* ------------------------------------------------------------------- consent */
function renderConsent(section, data, refresh) {
  if (!section) return;
  const consent = data.consent || {};
  if (!consent.required || consent.granted) {
    section.innerHTML = '';
    return;
  }
  // Rebuilding this card on every poll would clear a tick the user just made.
  if (section.querySelector('#consentBox')) return;

  section.innerHTML = `
    <article class="card card--pad">
      <h3>${icon('shield', 17)} Permission to check your documents</h3>
      <p class="text-muted text-sm">Before RideMate can check a licence, RC or
         insurance paper, we need your permission. A verification provider checks
         the document you upload against the details on your profile. We keep the
         result and the last four digits only, never a full document number.</p>
      <label class="flex items-start gap-8 mt-12 text-sm" style="cursor:pointer">
        <input type="checkbox" id="consentBox" class="mt-4" />
        <span>I agree to RideMate checking the documents I upload for this
          purpose (consent text ${h(consent.text_version || '')}).</span>
      </label>
      <button class="btn btn-primary btn-sm mt-12" id="consentBtn" disabled>
        ${icon('check', 14)} Allow document checks
      </button>
    </article>`;

  const box = section.querySelector('#consentBox');
  const btn = section.querySelector('#consentBtn');
  box.addEventListener('change', () => { btn.disabled = !box.checked; });
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'Saving…';
    try {
      await Api.recordKycConsent();
      toast('Permission saved.', 'success');
      await refresh();
    } catch (err) {
      toast(errMsg(err), 'error');
      btn.disabled = false;
      btn.textContent = 'Allow document checks';
    }
  });
}

/* ------------------------------------------------------------------ identity */
function renderIdentity(section, data, refresh) {
  if (!section) return;
  const existingForm = section.querySelector('#idForm');
  // Never rebuild the form under the user's cursor: a poll landing mid-typing
  // would wipe the document number they are entering.
  if (existingForm && existingForm.contains(document.activeElement)) {
    section.querySelector('#idStatusSlot').innerHTML = identityHead(data.kyc);
    return;
  }

  const kyc = data.kyc;
  const meta = STATUS[kyc.kyc_status] || STATUS.unverified;
  const canSubmit = kyc.can_submit;
  const canWithdraw = kyc.has_document && kyc.kyc_status !== 'submitted';
  const reviewer = h(reviewerName(data));

  section.innerHTML = `
    <article class="card card--pad">
      <div class="flex items-start justify-between gap-12">
        <div>
          <h3>${icon('user', 17)} Identity check</h3>
          <p class="text-muted text-sm">Driving licence or Aadhaar. Only the last four
             digits are stored; ${reviewer} reviews the document image, never a plain
             number.</p>
        </div>
        <span id="idStatusSlot">${identityHead(kyc)}</span>
      </div>
      ${kyc.has_document ? `
        <div class="grid-2 mt-12 text-sm">
          <div><span class="text-faint">Document</span><br><b>${h(DOC_LABELS[kyc.doc_type] || kyc.doc_type || '—')}</b></div>
          <div><span class="text-faint">Number</span><br><b>${h(kyc.doc_number_masked || '—')}</b></div>
          <div><span class="text-faint">Submitted</span><br><b>${h(fmt(kyc.submitted_at))}</b></div>
          <div><span class="text-faint">Reviewed</span><br><b>${h(fmt(kyc.reviewed_at))}</b></div>
        </div>` : ''}
      ${kyc.kyc_status === 'submitted' ? `<p class="text-faint text-sm mt-12">${icon('clock', 13)} In ${reviewer}'s review queue. This page will switch to verified without a reload.</p>` : ''}
      ${kyc.kyc_status === 'rejected' && kyc.reason ? `<div class="mt-12" style="background:var(--red-soft);padding:10px 12px;border-radius:10px"><b class="text-sm">Reviewer note</b><p class="text-sm">${h(kyc.reason)}</p></div>` : ''}
      ${kyc.kyc_status === 'verified' ? `<p class="text-sm mt-12">${icon('check', 14)} Identity verified by ${reviewer}. ${data.vehicles.some((v) => v.verification_status === 'verified') ? 'Vehicle also verified — publishing is unlocked.' : 'Next: get a vehicle verified.'}</p>` : ''}

      ${canSubmit ? identityForm(data.docTypes) : ''}

      ${canWithdraw ? `<button class="btn btn-outline btn-sm mt-12" data-withdraw>${icon('trash', 14)} Withdraw document</button>` : ''}
      <span id="idState" data-state="${meta.label}" hidden></span>
    </article>`;

  const form = section.querySelector('#idForm');
  if (form) {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const btn = form.querySelector('button[type="submit"]');
      const fields = {
        doc_type: form.elements.doc_type.value,
        doc_number: form.elements.doc_number.value.trim(),
        full_name: form.elements.full_name.value.trim(),
        date_of_birth: form.elements.date_of_birth.value,
      };
      const file = form.elements.file.files[0];
      if (!file) { toast('Attach a photo or PDF of the document.', 'error'); return; }
      if (!fields.date_of_birth) { toast('Enter your date of birth.', 'error'); return; }
      if (!isAdult(fields.date_of_birth)) {
        toast('You must be at least 18 years old to verify a driving licence.', 'error');
        return;
      }
      btn.disabled = true;
      btn.textContent = 'Submitting…';
      try {
        await Api.submitIdentity(fields, file);
        toast(`Document submitted — ${reviewerName(data)} will review it.`, 'success');
        await refresh();
      } catch (err) {
        toast(errMsg(err), 'error');
        btn.disabled = false;
        btn.textContent = 'Submit for verification';
      }
    });
  }

  const withdraw = section.querySelector('[data-withdraw]');
  if (withdraw) {
    withdraw.addEventListener('click', async () => {
      if (!window.confirm('Withdraw your identity document? You will need to upload it again.')) return;
      withdraw.disabled = true;
      try {
        await Api.withdrawIdentity();
        toast('Document withdrawn.', 'success');
        await refresh();
      } catch (err) {
        toast(errMsg(err), 'error');
        withdraw.disabled = false;
      }
    });
  }
}

function identityHead(kyc) {
  const state = kyc.kyc_status || 'unverified';
  return badge(state);
}

/* The API names the reviewing party (`review.reviewer`) so the copy cannot drift
   from the backend's actual workflow. Fall back to the same wording if an older
   server does not send it. */
function reviewerName(data) {
  return data?.review?.reviewer || "RideMate's Trust & Safety team";
}

function identityForm(docTypes) {
  const types = docTypes && docTypes.length ? docTypes : ['dl', 'aadhaar'];
  return `
    <form id="idForm" class="mt-16" novalidate>
      <div class="grid-2">
        <div class="field"><label>Document type</label>
          <select class="select" name="doc_type">
            ${types.map((t) => `<option value="${h(t)}">${h(DOC_LABELS[t] || t)}</option>`).join('')}
          </select></div>
        <div class="field"><label>Document number</label>
          <input class="input" name="doc_number" required minlength="4" maxlength="32" placeholder="KA0519930123456" /></div>
      </div>
      <div class="grid-2">
        <div class="field"><label>Full name (as printed)</label>
          <input class="input" name="full_name" maxlength="120" placeholder="Kamaresh E" /></div>
        <div class="field"><label>Date of birth</label>
          <input class="input" type="date" name="date_of_birth" max="${adultCutoff()}" required /></div>
      </div>
      <div class="field"><label>Photo or PDF of the document</label>
        <input class="input" type="file" name="file" accept=".pdf,.png,.jpg,.jpeg,.webp" required /></div>
      <button class="btn btn-primary mt-12" type="submit">Submit for verification</button>
    </form>`;
}

/* ------------------------------------------------------------------- vehicles */
function renderVehicles(section, data, refresh) {
  if (!section) return;
  const rows = data.vehicles;
  const consentBlocked = data.consent && data.consent.required && !data.consent.granted;

  const body = rows.length ? rows.map((v) => {
    const state = v.verification_status || 'unverified';
    const missing = (v.kyc?.missing || []).map((m) => m.replaceAll('_', ' ')).join(', ');
    const auto = v.kyc?.auto_check;
    const autoMeta = auto && AUTO[auto.status];
    const autoReasons = (auto?.reasons || []).filter(Boolean);
    return `
      <article class="card card--pad">
        <div class="flex items-start justify-between gap-12">
          <div>
            <h3>${h(v.vehicle_model || 'Vehicle')}</h3>
            <p class="text-muted text-sm">${h(v.vehicle_number)} · ${h(v.vehicle_type)} · ${v.seat_count} seats</p>
          </div>
          ${badge(state)}
        </div>
        <div class="flex gap-8 mt-12" style="flex-wrap:wrap">
          <span class="badge ${v.dl_document_status === 'uploaded' ? 'badge--success' : 'badge--ghost'}">${icon('upload', 12)} licence ${v.dl_document_status === 'uploaded' ? 'on file' : 'not added'}</span>
          <span class="badge ${v.insurance_document_status === 'uploaded' ? 'badge--success' : 'badge--ghost'}">insurance ${v.insurance_document_status === 'uploaded' ? 'on file' : 'not added'}</span>
          <span class="badge ${v.rc?.rc_status === 'approved' ? 'badge--success' : 'badge--ghost'}">RC ${h(v.rc?.rc_status || 'not submitted')}</span>
        </div>
        ${autoMeta ? `<div class="flex items-center gap-8 mt-12" style="flex-wrap:wrap">
          <span class="badge ${autoMeta.cls}">${icon('shield', 12)} ${autoMeta.label}</span>
          ${auto.status === 'pass' && state === 'submitted' ? '<span class="text-faint text-sm">Waiting for final review.</span>' : ''}
          ${auto.status === 'unavailable' ? '<span class="text-faint text-sm">The check service was unavailable; a reviewer will look.</span>' : ''}
        </div>` : ''}
        ${autoReasons.length ? `<ul class="text-sm mt-8" style="color:var(--red);padding-left:18px">${autoReasons.map((r) => `<li>${h(r)}</li>`).join('')}</ul>` : ''}
        ${missing ? `<p class="text-faint text-sm mt-8">Still needed: ${h(missing)}</p>` : ''}
        ${state === 'rejected' && v.kyc?.reason ? `<p class="text-sm mt-8" style="color:var(--red)">${h(v.kyc.reason)}</p>` : ''}
        <div class="flex gap-8 mt-12" style="flex-wrap:wrap">
          <a class="btn btn-outline btn-sm" href="#/vehicles">${icon('edit', 14)} Manage documents</a>
          ${v.kyc?.can_submit ? `<button class="btn btn-primary btn-sm" data-submit="${h(v.id)}" ${consentBlocked ? 'disabled title="Allow document checks first"' : ''}>Submit for review</button>` : ''}
        </div>
        ${consentBlocked && v.kyc?.can_submit ? '<p class="text-faint text-sm mt-8">Give permission for document checks above to submit.</p>' : ''}
      </article>`;
  }).join('') : emptyState({
    ic: 'car', title: 'No vehicle on the account',
    desc: 'Add a car, bike or auto and upload its licence and insurance, then submit it for review.',
    action: '<a class="btn btn-primary btn-sm mt-12" href="#/vehicles">Add a vehicle</a>',
  });

  section.innerHTML = `
    <div class="section-title">Vehicles</div>
    <p class="text-faint text-sm" style="margin-bottom:12px">Licence, insurance and RC
       papers are reviewed by ${h(reviewerName(data))} (admin/support). A vehicle
       decision is separate from your identity check above.</p>
    <div class="flex flex-col gap-12">${body}</div>`;

  section.querySelectorAll('[data-submit]').forEach((el) => {
    el.addEventListener('click', async () => {
      el.disabled = true;
      try {
        await Api.submitVehicleVerification(el.dataset.submit);
        toast('Vehicle submitted for review.', 'success');
        await refresh();
      } catch (err) {
        toast(errMsg(err), 'error');
        el.disabled = false;
      }
    });
  });
}

function fmt(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}
