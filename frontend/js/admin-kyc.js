/* Admin-only manual review queues for vehicle DL/insurance and RC documents. */
import { Api, store } from './api.js';
import { h, toast, errMsg, icon, emptyState } from './ui.js';

const documentLabels = { dl: 'Driving licence', insurance: 'Insurance', rc: 'Registration certificate (RC)' };

/* Automated-check result badge. Neutral wording only: these are document
   checks against the supplied paperwork, not a claim of government verification. */
const AUTO_LABEL = {
  checking: { text: 'Auto-check running', cls: 'badge--brand' },
  pass: { text: 'Auto-check passed', cls: 'badge--success' },
  fail: { text: 'Auto-check found issues', cls: 'badge--danger' },
  inconclusive: { text: 'Auto-check inconclusive', cls: 'badge--warn' },
  unavailable: { text: 'Auto-check unavailable', cls: 'badge--warn' },
};

function autoCheckBlock(auto) {
  if (!auto) {
    return '<p class="text-faint text-sm mt-8">No automated check was run for this packet.</p>';
  }
  const meta = AUTO_LABEL[auto.status] || { text: 'Auto-check result', cls: 'badge--ghost' };
  const reasons = (auto.reasons || []).filter(Boolean);
  const failedChecks = Object.entries(auto.checks || {})
    .filter(([, value]) => value === 'fail' || value === 'unknown' || value === 'flag')
    .map(([name, value]) => `${name.replaceAll('_', ' ')}: ${value}`);
  return `
    <div class="mt-12" style="background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 12px">
      <div class="flex items-center gap-8" style="flex-wrap:wrap">
        <span class="badge ${meta.cls}">${icon('shield', 12)} ${meta.text}</span>
        ${auto.provider ? `<span class="text-faint text-sm">provider: ${h(auto.provider)}</span>` : ''}
        ${auto.checked_at ? `<span class="text-faint text-sm">· ${h(auto.checked_at)}</span>` : ''}
      </div>
      ${reasons.length ? `<ul class="text-sm mt-8" style="padding-left:18px">${reasons.map((r) => `<li>${h(r)}</li>`).join('')}</ul>` : ''}
      ${failedChecks.length ? `<p class="text-faint text-sm mt-8">Checks needing a person: ${h(failedChecks.join('; '))}</p>` : ''}
    </div>`;
}

function licenceOcrBlock(check) {
  if (!check) return '';
  const label = check.status === 'checking'
    ? 'Licence OCR running'
    : check.status === 'checked'
      ? 'Licence OCR looks good'
      : check.status === 'unavailable'
        ? 'Licence OCR unavailable'
        : 'Licence OCR needs review';
  const cls = check.status === 'checked' ? 'badge--success'
    : check.status === 'checking' ? 'badge--brand' : 'badge--warn';
  const reasons = (check.reasons || []).map((reason) => ({
    image_blurry: 'Photo is blurry',
    licence_no_not_found: 'Licence number could not be read',
    licence_no_mismatch: 'Licence number differs from the entered value',
    expiry_not_found: 'Expiry date could not be read',
    expired: 'Licence appears expired',
    name_mismatch: 'Name may not match the driver profile',
    bad_file_type: 'Licence image type is unsupported',
    unreadable: 'Photo could not be read',
  }[reason] || reason));
  return `
    <div class="mt-8">
      <span class="badge ${cls}">${icon('file-text', 12)} ${label}</span>
      ${check.checked_at ? `<span class="text-faint text-sm"> · ${h(check.checked_at)}</span>` : ''}
      ${reasons.length ? `<p class="text-sm mt-8">${h(reasons.join('; '))}</p>` : ''}
      <p class="text-faint text-xs mt-8">OCR is advisory; the admin makes the final decision.</p>
    </div>`;
}

export async function renderAdminKyc(root) {
  // Backend `require_admin` accepts both staff roles; the nav shows the queue to
  // support staff too, so the client gate must match or support agents land on a
  // dead end even though every API call they make would be allowed.
  const role = store.user?.role;
  if (!['admin', 'support'].includes(role)) {
    root.innerHTML = emptyState({ ic: 'shield', title: 'Reviewer access required',
      desc: 'Only authorised RideMate administrators and support reviewers can access identity documents.' });
    return;
  }
  root.innerHTML = `
    <div class="page-head"><div><div class="eyebrow">Trust & safety</div><h1>Driver document review</h1>
    <p>Review DL, insurance and RC documents before drivers can publish rides.</p>
    <p class="text-faint text-xs" style="margin-top:6px">Approvals and rejections are recorded against your account, with the reason, in the audit log.</p></div>
    <button class="btn btn-outline btn-sm" data-reload>${icon('refresh-cw', 15)} Refresh queues</button></div>
    <section class="mt-16"><div class="section-title">Driver identity documents</div><div id="idQueue" class="flex flex-col gap-12"><div class="skeleton" style="height:120px"></div></div></section>
    <section class="mt-16"><div class="section-title">Driving licence and insurance</div><div id="dlQueue" class="flex flex-col gap-12"><div class="skeleton" style="height:120px"></div></div></section>
    <section class="mt-16"><div class="section-title">Registration certificate (RC)</div><div id="rcQueue" class="flex flex-col gap-12"><div class="skeleton" style="height:120px"></div></div></section>`;
  root.querySelector('[data-reload]').addEventListener('click', load);

  function wireDocuments(scope) {
    scope.querySelectorAll('[data-doc-url]').forEach((button) => {
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const blob = await Api.downloadVehicleDoc(button.dataset.docUrl);
          const tab = window.open(URL.createObjectURL(blob), '_blank');
          if (tab) tab.opener = null;
          else toast('Your browser blocked the document tab. Allow pop-ups and try again.', 'error');
        } catch (err) { toast(errMsg(err), 'error'); }
        finally { button.disabled = false; }
      });
    });
    scope.querySelectorAll('[data-review]').forEach((button) => {
      button.addEventListener('click', async () => {
        const decision = button.dataset.decision;
        const reason = decision === 'reject' ? (window.prompt('Reason for rejection (required):') || '').trim() : '';
        if (decision === 'reject' && !reason) {
          toast('A rejection reason is required.', 'error');
          return;
        }
        button.disabled = true;
        try {
          const payload = { decision, ...(reason ? { reason } : {}) };
          if (button.dataset.review === 'rc') {
            await Api.reviewRc(button.dataset.id, payload);
          } else if (button.dataset.review === 'identity') {
            await Api.reviewIdentity(button.dataset.id, payload);
          } else {
            await Api.reviewVehicle(button.dataset.id, payload);
          }
          toast(decision === 'approve' ? 'Documents approved.' : 'Documents rejected with a reason.', 'success');
          await load();
        } catch (err) {
          toast(errMsg(err), 'error');
          button.disabled = false;
        }
      });
    });
  }

  async function load() {
    const idRoot = root.querySelector('#idQueue');
    const dlRoot = root.querySelector('#dlQueue');
    const rcRoot = root.querySelector('#rcQueue');
    idRoot.innerHTML = '<div class="skeleton" style="height:120px"></div>';
    dlRoot.innerHTML = '<div class="skeleton" style="height:120px"></div>';
    rcRoot.innerHTML = '<div class="skeleton" style="height:120px"></div>';
    try {
      const [idResult, dlResult, rcResult] = await Promise.all([
        Api.identityQueue('submitted'),
        Api.pendingVehicleReviews(),
        Api.rcQueue(),
      ]);
      const idRows = idResult.queue || [];
      const dlRows = dlResult.data || [];
      const rcRows = rcResult.queue || [];

      idRoot.innerHTML = idRows.length ? idRows.map((item) => `
        <article class="card card--pad">
          <div class="flex items-start justify-between gap-12"><div>
            <h3>${h(item.name || 'Driver')}</h3>
            <p class="text-muted text-sm">${h(item.email || '')}</p>
            <p class="text-sm mt-8"><b>${h(documentLabels[item.doc_type] || item.doc_type || 'Identity document')}</b>
              · number ${h(item.doc_number_masked || '—')}</p>
            ${item.legal_name ? `<p class="text-sm">Name on document: <b>${h(item.legal_name)}</b></p>` : ''}
            ${item.date_of_birth ? `<p class="text-sm">Date of birth: <b>${h(item.date_of_birth)}</b></p>` : ''}
            <p class="text-faint text-sm mt-8">Submitted: ${h(item.submitted_at || '—')}</p>
          </div><span class="badge badge--brand">Pending identity review</span></div>
          <div class="flex gap-8 mt-12" style="flex-wrap:wrap">
            ${item.document
              ? `<button class="btn btn-outline btn-sm" data-doc-url="/api/kyc/admin/identity/${h(item.user_id)}/document">${icon('file-text', 14)} View document</button>`
              : '<span class="badge badge--ghost">Document missing</span>'}
          </div>
          <div class="flex gap-8 mt-16">
            <button class="btn btn-primary btn-sm" data-review="identity" data-decision="approve" data-id="${h(item.user_id)}">Approve identity</button>
            <button class="btn btn-outline btn-sm" data-review="identity" data-decision="reject" data-id="${h(item.user_id)}">Reject</button>
          </div>
        </article>`).join('') : emptyState({ ic: 'check-circle', title: 'No identities waiting', desc: 'Driver identity submissions appear here for manual review.' });

      dlRoot.innerHTML = dlRows.length ? dlRows.map((item) => `
        <article class="card card--pad">
          <div class="flex items-start justify-between gap-12"><div>
            <h3>${h(item.owner.name || 'Driver')}</h3>
            <p class="text-muted text-sm">${h(item.owner.email)} ${item.owner.phone ? ' · ' + h(item.owner.phone) : ''}</p>
            <p class="text-sm mt-8"><b>${h(item.vehicle_number)}</b> · ${h(item.vehicle_model || item.vehicle_type || 'Vehicle')}</p>
          </div><span class="badge badge--ghost">Pending review</span></div>
          <div class="grid-2 mt-12">
            <div class="text-sm"><span class="text-faint">DL number</span><br><b>${h(item.dl_number || 'Not provided')}</b></div>
            <div class="text-sm"><span class="text-faint">Insurance policy</span><br><b>${h(item.insurance_number || 'Not provided')}</b></div>
            <div class="text-sm"><span class="text-faint">Submitted</span><br><b>${h(item.submitted_at || '—')}</b></div>
          </div>
          ${autoCheckBlock(item.auto_check)}
          ${licenceOcrBlock(item.licence_ocr_check)}
          <div class="flex gap-8 mt-12" style="flex-wrap:wrap">
            ${['dl', 'insurance'].map((kind) => item.documents[kind]
              ? `<button class="btn btn-outline btn-sm" data-doc-url="${h(item.documents[kind])}">${icon('file-text', 14)} View ${documentLabels[kind]}</button>`
              : `<span class="badge badge--ghost">${documentLabels[kind]} missing</span>`).join('')}
          </div>
          <div class="flex gap-8 mt-16">
            <button class="btn btn-primary btn-sm" data-review="vehicle" data-decision="approve" data-id="${h(item.id)}">Approve DL/insurance</button>
            <button class="btn btn-outline btn-sm" data-review="vehicle" data-decision="reject" data-id="${h(item.id)}">Reject</button>
          </div>
        </article>`).join('') : emptyState({ ic: 'check-circle', title: 'No DL/insurance packets waiting', desc: 'Submitted packets appear here for manual review.' });

      rcRoot.innerHTML = rcRows.length ? rcRows.map((item) => `
        <article class="card card--pad">
          <div class="flex items-start justify-between gap-12"><div>
            <h3>${h(item.owner?.name || 'Driver')}</h3>
            <p class="text-muted text-sm">${h(item.owner?.email || '')} ${item.owner?.phone ? ' · ' + h(item.owner.phone) : ''}</p>
            <p class="text-sm mt-8"><b>${h(item.vehicle_number || 'Vehicle')}</b> · RC number ${h(item.rc_number_masked || 'not available')}</p>
            <p class="text-faint text-sm mt-8">Submitted: ${h(item.submitted_at || '—')}</p>
          </div><span class="badge badge--ghost">Pending RC review</span></div>
          <div class="flex gap-8 mt-12">
            <button class="btn btn-outline btn-sm" data-doc-url="/api/kyc/vehicles/${h(item.vehicle_id)}/rc/document">${icon('file-text', 14)} View RC</button>
            <button class="btn btn-primary btn-sm" data-review="rc" data-decision="approve" data-id="${h(item.vehicle_id)}">Approve RC</button>
            <button class="btn btn-outline btn-sm" data-review="rc" data-decision="reject" data-id="${h(item.vehicle_id)}">Reject RC</button>
          </div>
        </article>`).join('') : emptyState({ ic: 'check-circle', title: 'No RCs waiting', desc: 'Registration certificates have a separate encrypted review queue.' });
      wireDocuments(dlRoot);
      wireDocuments(rcRoot);
      wireDocuments(idRoot);
    } catch (err) {
      const message = emptyState({ ic: 'alert', title: 'Could not load review queues', desc: errMsg(err) });
      idRoot.innerHTML = message;
      dlRoot.innerHTML = message;
      rcRoot.innerHTML = message;
    }
  }
  await load();
}
