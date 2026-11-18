/* Profile: avatar, stats, editable details, password change. */

import { Api, store } from './api.js';
import { isFirebaseEnabled, startFirebasePhoneVerification, confirmFirebasePhoneVerification } from './firebase-email-link.js';
import {
  icon, h, money, toast, errMsg, skeleton, rating, avatar,
  bindForm, fieldError, confirmDialog,
} from './ui.js';

export async function renderProfile(root) {
  root.innerHTML = `<div class="page-head"><h1>Profile</h1></div>${skeleton(2)}`;

  const me = store.user;
  const [statsRes] = await Promise.all([
    Api.stats().catch(() => null),
  ]);
  const stats = (statsRes && statsRes.stats) || {};

  root.innerHTML = `
    <div class="page-head"><div><div class="eyebrow">Your account</div><h1>Profile</h1>
    <p>Manage how riders and drivers see you.</p></div></div>

    <div class="card profile-head">
      <div class="avatar-wrap">
        ${avatar(me, 76, 'avatar--xl')}
        <label class="cam" title="Change photo">${icon('camera', 15)}
          <input type="file" id="avatarInput" accept="image/*" hidden /></label>
      </div>
      <div class="meta">
        <h2>${h(me.name)}</h2>
        <div class="text-faint text-sm">${h(me.email)}${me.phone ? ' · ' + h(me.phone) : ''}</div>
        <div class="flex gap-8 items-center mt-8" style="flex-wrap:wrap">
          ${rating(me.rating)}
          <span class="badge badge--ghost">${(me.total_rides || 0)} rides hosted</span>
          ${me.photo_url ? `<button type="button" class="btn btn-ghost btn-sm" id="avatarRemove">${icon('trash', 13)} Remove photo</button>` : ''}
        </div>
      </div>
      <div style="margin-left:auto" class="stats-head">
        <div class="flex gap-16 text-center">
          <div><b class="fw-800" style="font-family:var(--font-display);font-size:22px">${stats.rides_taken || 0}</b><div class="text-faint text-xs">Trips taken</div></div>
          <div><b class="fw-800" style="font-family:var(--font-display);font-size:22px">${stats.rides_given || 0}</b><div class="text-faint text-xs">Rides given</div></div>
        </div>
      </div>
    </div>

    <div class="stat-grid mt-16">
      ${statCard('wallet', 'Total earned', money(stats.total_earnings || 0))}
      ${statCard('leaf', 'CO₂ saved', `${(stats.co2_saved_kg || 0).toLocaleString('en-IN')} kg`)}
      ${statCard('star', 'Average rating', (me.rating || 0) ? (me.rating).toFixed(1) : 'New')}
      ${statCard('users', 'Completed trips', (stats.taken_completed || 0) + (stats.given_completed || 0))}
    </div>

    <section class="card card--pad mt-16" id="identityCard" aria-live="polite">
      <div class="flex items-center justify-between gap-12" style="flex-wrap:wrap">
        <div>
          <div class="section-title" style="margin:0">${icon('shield', 16)} Identity verification</div>
          <p class="text-faint text-sm" style="margin-top:4px">Every document is reviewed by RideMate's Trust &amp; Safety team (an admin). Nothing is approved automatically — this panel updates on its own.</p>
        </div>
        <span class="badge badge--ghost" id="identityBadge">${icon('clock', 13)} checking…</span>
      </div>
      <div id="identityBody" class="mt-12"></div>
    </section>

    <div class="detail-grid mt-16" style="align-items:stretch">
      <div class="card card--pad">
        <div class="section-title" style="margin-top:0">Personal details</div>
        <form id="editForm">
          <div class="flex flex-col gap-12">
            <div class="field"><label for="pf-name">Full name</label>
              <input class="input" id="pf-name" name="name" value="${h(me.name)}" required /></div>
            <div class="grid-2">
              <div class="field"><label for="pf-age">Age</label>
                <input class="input" id="pf-age" name="age" type="number" min="5" max="120" value="${h(me.age || '')}" placeholder="—" /></div>
              <div class="field"><label for="pf-phone">Phone</label>
                <input class="input" id="pf-phone" name="phone" type="tel" value="${h(me.phone || '')}" placeholder="+91 …" /></div>
            </div>
            <div class="field"><label for="pf-gender">Gender</label>
              <select class="select" id="pf-gender" name="gender">
                <option value="" ${!me.gender ? 'selected' : ''}>Prefer not to say</option>
                <option value="male" ${me.gender === 'male' ? 'selected' : ''}>Male</option>
                <option value="female" ${me.gender === 'female' ? 'selected' : ''}>Female</option>
                <option value="other" ${me.gender === 'other' ? 'selected' : ''}>Other</option>
              </select></div>
            <div class="field"><label for="pf-bio">Bio</label>
              <textarea class="textarea" id="pf-bio" name="bio" maxlength="300" placeholder="A short line riders see before they book…">${h(me.bio || '')}</textarea></div>
            <button type="submit" class="btn btn-primary">Save changes</button>
          </div>
        </form>
      </div>

      <div class="detail-stack">
        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Email verification</div>
          <p class="text-faint text-sm">Request a verification code by email, then enter it here to verify your account.</p>
          <button type="button" class="btn btn-outline btn-block" id="sendVerification">Send verification email</button>
          <form id="verifyEmailForm" class="mt-12">
            <div class="field"><label for="verifyEmailToken">Verification code</label>
              <input class="input" id="verifyEmailToken" name="token" autocomplete="one-time-code" required maxlength="64" placeholder="Paste the code from your email" /></div>
            <button type="submit" class="btn btn-primary btn-block">Verify email</button>
          </form>
        </div>
        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Phone verification</div>
          <p class="text-faint text-sm" id="phoneVerificationStatus">${me.phone_verified ? 'Your phone number is verified.' : 'Verify that you can receive SMS messages on this phone.'}</p>
          <div class="field">
            <label for="phoneOtpNumber">Phone number (international format)</label>
            <input class="input" id="phoneOtpNumber" type="tel" autocomplete="tel" value="${h(me.phone || '')}" placeholder="+919876543210" ${me.phone_verified ? 'disabled' : ''} />
          </div>
          <div id="phoneOtpRecaptcha" class="mt-12"></div>
          <button type="button" class="btn btn-outline btn-block mt-12" id="sendPhoneOtp" ${me.phone_verified ? 'disabled' : ''}>Send SMS code</button>
          <form id="phoneOtpForm" class="mt-12" hidden>
            <div class="field"><label for="phoneOtpCode">6-digit SMS code</label>
              <input class="input" id="phoneOtpCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></div>
            <button type="submit" class="btn btn-primary btn-block">Verify phone</button>
          </form>
          <p class="text-faint text-xs mt-12">Requires Firebase Phone Authentication to be enabled for this project. SMS rates and regional restrictions may apply.</p>
        </div>
        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Change password</div>
          <form id="pwForm">
            <div class="flex flex-col gap-12">
              <div class="field"><label>Current password</label>
                <input class="input" type="password" name="current_password" required autocomplete="current-password" /></div>
              <div class="field"><label>New password</label>
                <input class="input" type="password" name="new_password" required autocomplete="new-password" placeholder="8+ chars, letter + number" /></div>
              <div class="field"><label>Confirm password</label>
                <input class="input" type="password" name="confirm" required autocomplete="new-password" /></div>
              <button type="submit" class="btn btn-outline">Update password</button>
            </div>
          </form>
        </div>

        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Session</div>
          <button class="btn btn-danger btn-block" data-logout>${icon('logout', 16)} Log out of RideMate</button>
        </div>
      </div>
    </div>`;

  /* Phone OTP: Firebase sends and verifies the SMS; the backend independently
     verifies the signed ID token and only then sets phone_verified=true. */
  const phoneSend = root.querySelector('#sendPhoneOtp');
  const phoneForm = root.querySelector('#phoneOtpForm');
  if (phoneSend && !me.phone_verified) {
    phoneSend.addEventListener('click', async () => {
      try {
        if (!(await isFirebaseEnabled())) {
          throw new Error('Firebase is not configured on this deployment yet.');
        }
        const phone = root.querySelector('#phoneOtpNumber').value;
        await startFirebasePhoneVerification(phone, root.querySelector('#phoneOtpRecaptcha'));
        phoneForm.hidden = false;
        phoneSend.disabled = true;
        phoneSend.textContent = 'SMS code sent';
        toast('Enter the SMS code sent to your phone.', 'success');
      } catch (err) { toast(errMsg(err), 'error'); }
    });
    bindForm(phoneForm, async (form) => {
      try {
        const idToken = await confirmFirebasePhoneVerification(root.querySelector('#phoneOtpCode').value);
        const res = await Api.verifyPhone({ id_token: idToken });
        store.user = res.user;
        toast('Phone number verified successfully.', 'success');
        renderProfile(root);
      } catch (err) {
        // A wrong/expired code should not lock the user out of retrying.
        phoneSend.disabled = false;
        phoneSend.textContent = 'Resend SMS code';
        toast(errMsg(err), 'error');
      }
    });
  }

  /* Identity KYC: riders and captains submit here; an admin decides in the
     RideMate maintenance console. Nothing on this side can approve itself. */
  wireIdentity(root, me);

  /* Email verification */
  root.querySelector('#sendVerification').addEventListener('click', async () => {
    try {
      await Api.sendVerificationEmail();
      toast('Verification email requested. Check your inbox and spam folder.', 'success');
    } catch (err) { toast(errMsg(err), 'error'); }
  });
  bindForm(root.querySelector('#verifyEmailForm'), async (form) => {
    const token = new FormData(form).get('token');
    try {
      const res = await Api.confirmEmail({ token });
      store.user = res.user;
      toast('Email verified successfully.', 'success');
      renderProfile(root);
    } catch (err) { toast(errMsg(err), 'error'); }
  });

  /* avatar upload */
  root.querySelector('#avatarInput').addEventListener('change', async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    try {
      const res = await Api.uploadAvatar(file);
      store.user = res.user;
      window.dispatchEvent(new CustomEvent('ridemate:user-updated'));
      toast('Profile photo updated.', 'success');
      renderProfile(root);
    } catch (err) {
      toast(errMsg(err), 'error');
    }
  });

  /* avatar remove */
  root.querySelector('#avatarRemove')?.addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const ok = await confirmDialog({
      title: 'Remove profile photo?',
      message: 'Your avatar will show your initials again. You can upload a new photo whenever you like.',
      confirmLabel: 'Remove photo',
      danger: true,
    });
    if (!ok) return;
    btn.disabled = true;
    try {
      const res = await Api.removeAvatar();
      store.user = res.user;
      window.dispatchEvent(new CustomEvent('ridemate:user-updated'));
      toast('Profile photo removed.', 'success');
      renderProfile(root);
    } catch (err) {
      btn.disabled = false;
      toast(errMsg(err), 'error');
    }
  });

  /* edit profile */
  bindForm(root.querySelector('#editForm'), async (form) => {
    const data = new FormData(form);
    const payload = {
      name: data.get('name'),
      age: data.get('age') ? Number(data.get('age')) : '',
      phone: data.get('phone') || '',
      gender: data.get('gender') || '',
      bio: data.get('bio') || '',
    };
    try {
      const res = await Api.updateMe(payload);
      store.user = res.user;
      window.dispatchEvent(new CustomEvent('ridemate:user-updated'));
      toast('Profile updated.', 'success');
      renderProfile(root);
    } catch (err) { toast(errMsg(err), 'error'); }
  });

  /* change password */
  bindForm(root.querySelector('#pwForm'), async (form) => {
    const d = new FormData(form);
    if (d.get('new_password') !== d.get('confirm')) {
      fieldError(form.querySelector('[name=confirm]'), 'Passwords do not match.');
      return;
    }
    try {
      await Api.changePassword({ current_password: d.get('current_password'), new_password: d.get('new_password') });
      form.reset();
      toast('Password updated.', 'success');
    } catch (err) { toast(errMsg(err), 'error'); }
  });

  /* logout */
  root.querySelector('[data-logout]').addEventListener('click', async () => {
    const ok = await confirmDialog({ title: 'Log out?', message: 'You will need to sign back in to see your trips.', confirmLabel: 'Log out' });
    if (!ok) return;
    await Api.logout();
    store.clear();
    location.hash = '#/login';
  });
}

function statCard(ic, label, value) {
  return `<div class="card stat-card">${icon(ic, 22)}<b>${value ?? '—'}</b><span>${label}</span></div>`;
}

/* ------------------------------------------------------- identity KYC card */
const ID_LABELS = { aadhaar: 'Aadhaar', passport: 'Passport', voter_id: 'Voter ID', dl: 'Driving licence' };
const ID_MAX_BYTES = 5 * 1024 * 1024;
const ID_POLL_MS = 5000;

function idTypes(data) {
  const allowed = (data && data.allowed_document_types) || [];
  return allowed.length ? allowed : ['aadhaar', 'passport', 'voter_id', 'dl'];
}

function fmtKycTime(value) {
  if (!value) return '';
  const when = new Date(value);
  if (Number.isNaN(when.getTime())) return '';
  return when.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' });
}

function identityFormHtml(types) {
  const cutoff = new Date();
  cutoff.setFullYear(cutoff.getFullYear() - 18);
  return `
    <form data-id-form class="mt-8">
      <div class="grid-2">
        <div class="field"><label for="idDocType">Document type</label>
          <select class="select" id="idDocType" name="doc_type" required>
            ${types.map((t) => `<option value="${h(t)}">${h(ID_LABELS[t] || t)}</option>`).join('')}
          </select></div>
        <div class="field"><label for="idDocNumber">Document number</label>
          <input class="input" id="idDocNumber" name="doc_number" required autocomplete="off"
                 placeholder="Exactly as printed" /></div>
      </div>
      <div class="grid-2">
        <div class="field"><label for="idName">Name on document</label>
          <input class="input" id="idName" name="full_name" autocomplete="name" /></div>
        <div class="field"><label for="idDob">Date of birth</label>
          <input class="input" id="idDob" name="date_of_birth" type="date" max="${cutoff.toISOString().slice(0, 10)}" /></div>
      </div>
      <div class="field"><label for="idFile">Document photo or PDF</label>
        <input class="input" id="idFile" name="file" type="file" required
               accept=".pdf,.png,.jpg,.jpeg,.webp" /></div>
      <p class="text-faint text-xs">JPG, PNG, WebP or PDF up to 5 MB. Stored encrypted and visible only to RideMate reviewers.</p>
      <button type="submit" class="btn btn-primary mt-8">Submit for admin approval</button>
    </form>`;
}

function wireIdentity(root, me) {
  const card = root.querySelector('#identityCard');
  const body = root.querySelector('#identityBody');
  const badge = root.querySelector('#identityBadge');
  if (!card || !body || !badge) return;

  let timer = null;
  const alive = () => card.isConnected;
  const stopPolling = () => { if (timer) { clearInterval(timer); timer = null; } };
  const setBadge = (cls, ic, text) => {
    badge.className = `badge ${cls}`;
    badge.innerHTML = `${icon(ic, 13)} ${h(text)}`;
  };

  const viewDocButton = (kyc) => (kyc.has_document
    ? `<button type="button" class="btn btn-ghost btn-sm" data-id-doc>${icon('eye', 14)} View uploaded document</button>`
    : '');

  const withdrawButton = (kyc, state) => (kyc.has_document && state !== 'submitted'
    ? `<button type="button" class="btn btn-ghost btn-sm" data-id-withdraw>${icon('trash', 14)} Remove document</button>`
    : '');

  const metaBadges = (kyc) => {
    const doc = kyc.doc_type ? `<span class="badge badge--ghost">${h(ID_LABELS[kyc.doc_type] || kyc.doc_type)}${kyc.doc_number_masked ? ' · ' + h(kyc.doc_number_masked) : ''}</span>` : '';
    return `<div class="flex gap-8 items-center mt-12" style="flex-wrap:wrap">${doc}${viewDocButton(kyc)}${withdrawButton(kyc, kyc.kyc_status)}</div>`;
  };

  const wireForm = (form) => {
    if (!form) return;
    const dob = form.querySelector('#idDob');
    if (dob && me.role === 'driver') {
      dob.required = true;
      dob.closest('.field').querySelector('label').textContent = 'Date of birth (required)';
    }
    form.querySelector('#idName').value = me.name || '';
    form.addEventListener('submit', (event) => {
      // The form is inside a hash-routed page: without preventDefault the
      // browser does a native GET submit and the upload is silently dropped.
      event.preventDefault();
      submitIdentity(event.currentTarget);
    });
  };

  const paint = (data) => {
    stopPolling();
    const kyc = data.kyc || {};
    const state = kyc.kyc_status || 'unverified';
    const reviewer = (data.review && data.review.reviewer) || 'RideMate Trust & Safety team';

    if (state === 'verified') {
      setBadge('badge--success', 'check', 'Verified');
      body.innerHTML = `
        <p class="text-sm" style="color:var(--green)">${icon('check', 14)} Approved by ${h(reviewer)}${kyc.reviewed_at ? ` · ${h(fmtKycTime(kyc.reviewed_at))}` : ''}.</p>
        ${metaBadges(kyc)}
        <p class="text-faint text-xs mt-12">Your identity is on file. Uploading a new document re-opens the check.</p>
        <button type="button" class="btn btn-outline btn-sm mt-8" data-id-replace>${icon('upload', 14)} Upload a new document</button>
        <div data-id-replace-slot></div>`;
    } else if (state === 'submitted') {
      setBadge('badge--warn', 'clock', 'In review');
      body.innerHTML = `
        <p class="text-sm">${icon('clock', 14)} Your document is waiting for an admin to approve it${kyc.submitted_at ? ` (submitted ${h(fmtKycTime(kyc.submitted_at))})` : ''}. This panel refreshes every few seconds.</p>
        ${metaBadges(kyc)}
        <p class="text-faint text-xs mt-12">You cannot approve your own document. A RideMate admin makes the decision in the maintenance console.</p>`;
      timer = setInterval(async () => {
        if (!alive()) { stopPolling(); return; }
        try {
          const fresh = await Api.myKyc();
          if (fresh.kyc && fresh.kyc.kyc_status !== 'submitted') paint(fresh);
        } catch { /* keep polling; a transient network error is not a decision */ }
      }, ID_POLL_MS);
    } else if (state === 'rejected') {
      setBadge('badge--danger', 'x', 'Rejected');
      body.innerHTML = `
        <p class="text-sm" style="color:var(--red)">${icon('x', 14)} Your document was rejected${kyc.reviewed_at ? ` on ${h(fmtKycTime(kyc.reviewed_at))}` : ''}${kyc.reason ? `: ${h(kyc.reason)}` : '.'}</p>
        <p class="text-faint text-sm mt-8">Upload a clearer copy and submit again — an admin reviews every resubmission.</p>
        ${identityFormHtml(idTypes(data))}`;
    } else {
      setBadge('badge--ghost', 'shield', 'Not verified');
      body.innerHTML = `
        <p class="text-sm">Upload one government ID to verify who you are. An admin checks it, then rides unlock.</p>
        ${identityFormHtml(idTypes(data))}`;
    }

    const form = body.querySelector('[data-id-form]');
    if (form) wireForm(form);
    body.querySelector('[data-id-replace]')?.addEventListener('click', (event) => {
      const slot = body.querySelector('[data-id-replace-slot]');
      slot.innerHTML = identityFormHtml(idTypes(data));
      wireForm(slot.querySelector('[data-id-form]'));
      event.currentTarget.remove();
      slot.scrollIntoView({ block: 'center' });
    });
    body.querySelector('[data-id-doc]')?.addEventListener('click', openDocument);
    body.querySelector('[data-id-withdraw]')?.addEventListener('click', removeDocument);
  };

  const load = async () => {
    try {
      paint(await Api.myKyc());
    } catch (err) {
      setBadge('badge--ghost', 'alert', 'Unavailable');
      body.innerHTML = `<p class="text-sm text-faint">Could not load your identity status: ${h(errMsg(err))}</p>`;
    }
  };

  const submitIdentity = async (form) => {
    const file = form.querySelector('#idFile').files[0];
    if (!file) { toast('Choose a document file first.', 'error'); return; }
    if (file.size > ID_MAX_BYTES) { toast('That file is larger than 5 MB.', 'error'); return; }
    const fields = new FormData(form);
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = 'Submitting…';
    try {
      await Api.submitIdentity({
        doc_type: fields.get('doc_type'),
        doc_number: fields.get('doc_number'),
        full_name: fields.get('full_name'),
        date_of_birth: fields.get('date_of_birth'),
      }, file);
      toast('Submitted. A RideMate admin will review it.', 'success');
      await load();
    } catch (err) {
      button.disabled = false;
      button.textContent = 'Submit for admin approval';
      toast(errMsg(err), 'error');
    }
  };

  const openDocument = async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const blob = await Api.downloadVehicleDoc('/api/kyc/identity/document');
      const tab = window.open(URL.createObjectURL(blob), '_blank');
      if (tab) tab.opener = null;
      else toast('Your browser blocked the document tab. Allow pop-ups and try again.', 'error');
    } catch (err) {
      toast(errMsg(err), 'error');
    } finally {
      button.disabled = false;
    }
  };

  const removeDocument = async (event) => {
    const ok = await confirmDialog({
      title: 'Remove your identity document?',
      message: 'The stored copy is deleted and your status returns to not verified. You can upload a new document at any time.',
      confirmLabel: 'Remove document',
      danger: true,
    });
    if (!ok) return;
    event.currentTarget.disabled = true;
    try {
      await Api.withdrawIdentity();
      toast('Document removed.', 'info');
      await load();
    } catch (err) {
      toast(errMsg(err), 'error');
      event.currentTarget.disabled = false;
    }
  };

  load();
}
