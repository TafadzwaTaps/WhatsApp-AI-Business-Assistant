/* admin_outreach.js — superadmin "Onboarding" section (additive).
   Never-activated businesses, restart trial, WhatsApp/email outreach.
   Self-injects its nav item and section; relies on dashboard.js globals
   (apiFetch, toast, escHtml, showSection, buildSidebar, userRole). */
(function () {
  'use strict';
  var items = [];

  function esc(s) { return (typeof escHtml === 'function') ? escHtml(s == null ? '' : s) : String(s == null ? '' : s); }
  function say(m, err) { if (typeof toast === 'function') toast(m, !!err); }

  function ensureSection() {
    if (document.getElementById('section-admin-outreach')) return;
    var main = document.querySelector('main.main');
    if (!main) return;
    var sec = document.createElement('div');
    sec.id = 'section-admin-outreach';
    sec.className = 'section';
    sec.innerHTML =
      '<div class="topbar"><div class="page-title">Never <span class="purple">Activated</span></div>' +
      '<button class="btn btn-purple" onclick="adminOutreachLoad()">↻ Refresh</button></div>' +
      '<div class="panel full"><div class="panel-body">' +
      '<p style="font-size:12px;color:var(--muted);margin:0 0 10px;">Businesses with no products and no orders. ' +
      'WhatsApp opens your own WhatsApp with a message ready to send; Email sends from the portal.</p>' +
      '<label style="font-size:12px;">Signed up at least <input id="ao-min-days" type="number" min="0" value="0" ' +
      'style="width:60px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:4px 6px;color:var(--text);" ' +
      'onchange="adminOutreachLoad()"> days ago</label>' +
      '<div style="overflow-x:auto;margin-top:12px;"><table><thead><tr><th>Business</th><th>Signed up</th><th>Trial</th>' +
      '<th>Last contact</th><th>Contact</th><th>Actions</th></tr></thead>' +
      '<tbody id="ao-body"><tr><td colspan="6">Loading…</td></tr></tbody></table></div>' +
      '</div></div>';
    main.appendChild(sec);
  }

  function ensureNav() {
    if (typeof userRole === 'undefined' || userRole !== 'superadmin') return;
    var nav = document.getElementById('sidebar-nav');
    if (!nav || nav.querySelector('[data-ao-nav]')) return;
    var b = document.createElement('button');
    b.className = 'nav-item admin-item';
    b.setAttribute('data-ao-nav', '1');
    b.innerHTML = '<span class="icon">🤝</span> Onboarding';
    b.onclick = function () { showSection('admin-outreach', b); adminOutreachLoad(); if (typeof closeSidebar === 'function') closeSidebar(); };
    nav.appendChild(b);
  }

  function ago(i) {
    if (i.days_since_signup == null) return '—';
    return i.days_since_signup === 0 ? 'today' : i.days_since_signup + ' day' + (i.days_since_signup === 1 ? '' : 's') + ' ago';
  }
  function trialLabel(i) {
    var map = { trialing: 'Trial running', expired: 'Trial expired', paid: 'Paid' };
    var t = map[i.trial_state] || i.trial_state;
    return t + (i.trial_restarts ? ' (restarted ' + i.trial_restarts + '×)' : '');
  }
  function lastContact(i) {
    var l = i.last_outreach; if (!l || !l.at) return 'Never';
    return esc(l.channel || '') + ' · ' + esc(String(l.at).slice(0, 10));
  }

  window.adminOutreachLoad = async function () {
    ensureSection();
    var body = document.getElementById('ao-body'); if (!body) return;
    var minEl = document.getElementById('ao-min-days');
    var min = Math.max(0, parseInt(minEl && minEl.value, 10) || 0);
    try {
      var data = await apiFetch('/admin/outreach/never-activated?min_days=' + min);
      items = (data && data.items) || [];
    } catch (e) { body.innerHTML = '<tr><td colspan="6">Could not load: ' + esc(e.message) + '</td></tr>'; return; }
    if (!items.length) { body.innerHTML = '<tr><td colspan="6">Every business has added products or taken orders. 🎉</td></tr>'; return; }
    body.innerHTML = items.map(function (i, idx) {
      var wa = i.whatsapp_link
        ? '<a class="btn" target="_blank" rel="noopener" href="' + esc(i.whatsapp_link) + '" onclick="adminOutreachContacted(' + i.id + ',\'whatsapp\')">💬 WhatsApp</a>'
        : '<span style="color:var(--muted);font-size:11px;">no phone</span>';
      var em = i.email_link
        ? ' <a class="btn" href="' + esc(i.email_link) + '" onclick="adminOutreachContacted(' + i.id + ',\'email\')">✉️ Email app</a>'
        : '';
      return '<tr><td><b>' + esc(i.name) + '</b><div style="font-size:11px;color:var(--muted);">#' + i.id + ' · ' + esc(i.owner_email || 'no email') + '</div></td>' +
        '<td>' + esc(ago(i)) + '</td><td>' + esc(trialLabel(i)) + '</td><td>' + lastContact(i) + '</td>' +
        '<td>' + wa + em + '</td>' +
        '<td><button class="btn" onclick="adminOutreachCompose(' + idx + ')">📨 Send email</button> ' +
        '<button class="btn btn-purple" onclick="adminOutreachRestart(' + idx + ')">🔄 Restart trial</button></td></tr>';
    }).join('');
  };

  window.adminOutreachContacted = function (id, channel) {
    apiFetch('/admin/outreach/' + id + '/mark-contacted', { method: 'POST', body: JSON.stringify({ channel: channel }) })
      .catch(function () { /* best effort */ });
  };

  function modal(html) {
    var old = document.getElementById('ao-modal'); if (old) old.remove();
    var m = document.createElement('div');
    m.id = 'ao-modal';
    m.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9999;display:flex;align-items:center;justify-content:center;padding:16px;';
    m.innerHTML = '<div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;width:100%;max-width:480px;">' + html + '</div>';
    m.addEventListener('click', function (e) { if (e.target === m) m.remove(); });
    document.body.appendChild(m);
    return m;
  }
  var FIELD = 'width:100%;box-sizing:border-box;background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:9px 12px;color:var(--text);margin:6px 0 10px;font-family:inherit;';

  window.adminOutreachRestart = function (idx) {
    var i = items[idx]; if (!i) return;
    var m = modal('<h3 style="margin:0 0 8px;">Restart trial — ' + esc(i.name) + '</h3>' +
      '<label style="font-size:12px;">Reason (required, saved to the audit log)</label>' +
      '<textarea id="ao-reason" rows="3" style="' + FIELD + '" placeholder="e.g. Owner never got to set up; offered a fresh trial after a call"></textarea>' +
      '<label style="font-size:12px;">Trial length (days)</label>' +
      '<input id="ao-days" type="number" min="1" max="60" value="30" style="' + FIELD + '">' +
      '<div style="display:flex;gap:8px;justify-content:flex-end;"><button class="btn" id="ao-cancel">Cancel</button>' +
      '<button class="btn btn-purple" id="ao-go">Restart trial</button></div>');
    m.querySelector('#ao-cancel').onclick = function () { m.remove(); };
    m.querySelector('#ao-go').onclick = async function () {
      var reason = m.querySelector('#ao-reason').value.trim();
      var days = parseInt(m.querySelector('#ao-days').value, 10) || 30;
      if (reason.length < 5) { say('Please give a reason (at least 5 characters).', true); return; }
      try {
        await apiFetch('/admin/outreach/' + i.id + '/restart-trial', { method: 'POST', body: JSON.stringify({ reason: reason, days: days }) });
        say('Trial restarted for ' + i.name);
        m.remove(); adminOutreachLoad();
      } catch (e) { say(e.message, true); }
    };
  };

  window.adminOutreachCompose = function (idx) {
    var i = items[idx]; if (!i) return;
    var def = 'Hi,\n\nI saw that ' + (i.name || 'your business') + ' signed up for WaziBot but hasn\'t been set up yet. ' +
      'Do you need help adding your products or connecting WhatsApp? Reply to this email and I can set it up with you.\n\nThe WaziBot team';
    var m = modal('<h3 style="margin:0 0 8px;">Email ' + esc(i.name) + '</h3>' +
      '<div style="font-size:12px;color:var(--muted);">To: ' + esc(i.owner_email || 'no email on file') + '</div>' +
      '<input id="ao-subj" style="' + FIELD + '" value="Need a hand setting up WaziBot?">' +
      '<textarea id="ao-msg" rows="8" style="' + FIELD + '">' + esc(def) + '</textarea>' +
      '<div style="display:flex;gap:8px;justify-content:flex-end;"><button class="btn" id="ao-cancel">Cancel</button>' +
      '<button class="btn btn-purple" id="ao-send">Send email</button></div>');
    m.querySelector('#ao-cancel').onclick = function () { m.remove(); };
    m.querySelector('#ao-send').onclick = async function () {
      var btn = this; btn.disabled = true;
      try {
        await apiFetch('/admin/outreach/' + i.id + '/send-email', { method: 'POST',
          body: JSON.stringify({ subject: m.querySelector('#ao-subj').value, message: m.querySelector('#ao-msg').value }) });
        say('Email sent to ' + i.owner_email);
        m.remove(); adminOutreachLoad();
      } catch (e) { say(e.message, true); btn.disabled = false; }
    };
  };

  // Re-add the nav item every time dashboard.js rebuilds the sidebar.
  var orig = window.buildSidebar;
  if (typeof orig === 'function') {
    window.buildSidebar = function () {
      var r = orig.apply(this, arguments);
      try { ensureSection(); ensureNav(); } catch (e) { console.warn('admin_outreach nav', e); }
      return r;
    };
  }
  document.addEventListener('DOMContentLoaded', function () { try { ensureSection(); ensureNav(); } catch (e) {} });
  try { ensureSection(); ensureNav(); } catch (e) {}
})();
