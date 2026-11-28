/* Super Admin Security Center: containment (security audit Stage 4d). Every action asks for a
   reason and a confirmation. Uses dashboard.js globals and window.SecurityCenter. */
(function () {
  'use strict';

  const sc = window.SecurityCenter;
  const { byId, text, query, ticket, pager, emptyRow } = sc;
  const BASE = '/admin/saas/security';
  const state = { wired: false, page: 1, features: {} };
  const value = id => byId(id).value.trim();
  const words = v => String(v || '').replace(/_/g, ' ');
  const durationOf = id => { const s = byId(id); return (s.options && s.options[s.selectedIndex] || {}).text || `${s.value} minutes`; };

  function number(id, label, required) {
    const raw = value(id);
    if (!raw && !required) return null;
    if (!/^[1-9]\d{0,14}$/.test(raw)) throw new Error(`${label} must be a number.`);
    return Number(raw);
  }

  // One submission at a time; the confirmation comes after the input checks.
  async function act(form, build) {
    if (form.dataset.busy) return;
    form.dataset.busy = '1';
    try {
      const { question, path, body, done } = build();
      if (!window.confirm(question)) return;
      const result = await apiFetch(path, { method: 'POST', ...(body ? { body: JSON.stringify(body) } : {}) });
      if (!result) throw new Error('Not done: your session may have ended.');
      const warnings = result.warnings || [];
      toast(warnings.length ? `${done} ${warnings.join(' ')}` : done, warnings.length > 0);
      form.reset();
      refresh();
    } catch (error) {
      toast(error.message, true);
    } finally {
      delete form.dataset.busy;
    }
  }

  const blockAddress = () => {
    const ip = value('sc-c-ip');
    return {
      question: `Block ${ip} (${durationOf('sc-c-ip-minutes')})? Requests from this address will be refused.`,
      path: `${BASE}/restrictions`, done: 'Address blocked.',
      body: { kind: 'ip', target: ip, minutes: Number(value('sc-c-ip-minutes')), reason: value('sc-c-ip-reason'),
              incident_id: number('sc-c-ip-incident', 'Incident ID'), confirm: true },
    };
  };

  const restrictFeature = () => {
    const id = number('sc-c-f-business', 'Business ID', true);
    const feature = value('sc-c-feature');
    return {
      question: `Restrict ${state.features[feature] || words(feature)} for business #${id} (${durationOf('sc-c-f-minutes')})?`,
      path: `${BASE}/restrictions`, done: 'Feature restricted.',
      body: { kind: 'feature', target: String(id), feature, minutes: Number(value('sc-c-f-minutes')),
              reason: value('sc-c-f-reason'), incident_id: number('sc-c-f-incident', 'Incident ID'), confirm: true },
    };
  };

  const signOut = () => {
    const id = number('sc-c-s-business', 'Business ID', true);
    return {
      question: `Sign business #${id} out everywhere? Everyone using it must sign in again. The password is not changed.`,
      path: `${BASE}/businesses/${id}/sign-out`, done: 'Sessions ended.',
      body: { reason: value('sc-c-s-reason'), incident_id: number('sc-c-s-incident', 'Incident ID'), confirm: true },
    };
  };

  const suspendOrRestore = () => {
    const id = number('sc-c-u-business', 'Business ID', true);
    const suspend = value('sc-c-u-action') === 'suspend';
    const reason = value('sc-c-u-reason');
    if (!reason) throw new Error('Give a reason.');
    return {
      question: suspend ? `Suspend business #${id}? It cannot sign in until you restore it.`
        : `Restore business #${id}? This also cancels a pending account deletion.`,
      path: `/platform/businesses/${id}/${suspend ? 'suspend' : 'activate'}?${query({ reason })}`,
      done: suspend ? 'Business suspended.' : 'Business restored.',
    };
  };

  async function loadRestrictions(page) {
    const current = ticket('restrictions');
    state.page = page;
    const body = byId('sc-restrictions');
    try {
      const data = await apiFetch(`${BASE}/restrictions?${query({ include_ended: byId('sc-c-ended').checked ? 'true' : '', page })}`);
      if (!data || !current()) return;
      state.features = data.features || {};
      byId('sc-c-status').textContent = `Your address: ${data.your_address || 'unknown'} · Address blocking is ${data.ip_blocking_enabled ? 'on' : 'OFF on the server'}`;
      pager('sc-restrictions', page, data.has_more);
      if (!data.available) { body.innerHTML = emptyRow(7, data.problem || 'Unavailable.'); return; }
      const rows = data.restrictions || [];
      body.innerHTML = rows.length ? rows.map(r => {
        const ended = r.revoked_at ? 'lifted' : (new Date(r.expires_at) <= new Date() ? 'expired' : '');
        return `<tr>
          <td style="color:var(--text-dim);font-size:11px;">${escHtml(fmtTime(r.created_at))}</td>
          <td>${r.kind === 'ip' ? 'Address block' : text(state.features[r.feature] || words(r.feature))}</td>
          <td>${r.kind === 'ip' ? text(r.target) : 'business #' + escHtml(r.target)}</td>
          <td>${text(r.reason)}</td>
          <td style="font-size:11px;">${escHtml(fmtTime(r.expires_at))}</td>
          <td>${ended ? text(ended) : '<span class="badge badge-amber">active</span>'}</td>
          <td>${ended ? '—' : `<button type="button" class="btn btn-ghost" data-lift="${escHtml(r.id)}">Lift</button>`}</td>
        </tr>`;
      }).join('') : emptyRow(7, 'No restrictions to show.');
    } catch (error) {
      if (!current()) return;
      body.innerHTML = emptyRow(7, 'Unavailable — ' + error.message);
    }
  }

  async function lift(id) {
    const reason = (window.prompt('Why is this restriction being lifted?') || '').trim();
    if (!reason) return;
    try {
      const result = await apiFetch(`${BASE}/restrictions/${encodeURIComponent(id)}/revoke`, { method: 'POST', body: JSON.stringify({ reason }) });
      if (!result) throw new Error('Not done: your session may have ended.');
      const warnings = result.warnings || [];
      toast(warnings.length ? `Restriction lifted. ${warnings.join(' ')}` : 'Restriction lifted.', warnings.length > 0);
      refresh();
    } catch (error) { toast(error.message, true); }
  }

  function refresh() {
    loadRestrictions(state.page);
    sc.reloadOverview();
  }

  function wire() {
    if (state.wired) return;
    state.wired = true;
    const on = (id, type, handler) => byId(id).addEventListener(type, handler);
    [['sc-ip-form', blockAddress], ['sc-feature-form', restrictFeature], ['sc-signout-form', signOut], ['sc-suspend-form', suspendOrRestore]]
      .forEach(([id, build]) => on(id, 'submit', e => { e.preventDefault(); act(byId(id), build); }));
    on('sc-c-ended', 'change', () => loadRestrictions(1));
    on('sc-restrictions-prev', 'click', () => loadRestrictions(Math.max(1, state.page - 1)));
    on('sc-restrictions-next', 'click', () => loadRestrictions(state.page + 1));
    on('sc-restrictions', 'click', e => { const b = e.target.closest('[data-lift]'); if (b) lift(b.dataset.lift); });
    // From an event's detail view: fill the form in; the admin still gives a reason and confirms.
    on('sc-detail-block', 'click', () => {
      byId('sc-c-ip').value = byId('sc-detail-block').dataset.ip || '';
      byId('sc-detail-modal').classList.remove('open');
      byId('sc-ip-form').scrollIntoView({ block: 'center' });
      byId('sc-c-ip-reason').focus();
    });
  }

  window.loadSecurityContainment = function () {
    wire();
    loadRestrictions(1);
  };
})();
