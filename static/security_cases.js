/* Super Admin Security Center: incidents and vulnerability register (security audit Stage 4c).
   Uses apiFetch, escHtml, fmtTime and toast from dashboard.js and the helpers that
   security_center.js shares as window.SecurityCenter. No inline handlers. */
(function () {
  'use strict';

  const sc = window.SecurityCenter;
  const { byId, text, query, ticket, pager, emptyRow, badge } = sc;
  const BASE = '/admin/saas/security';
  const state = { wired: false, incidentsPage: 1, vulnPage: 1, incidents: [], vulns: [], open: null, linking: [], editing: null };
  const words = value => String(value || '').replace(/_/g, ' ');
  const value = id => byId(id).value.trim();
  const list = id => value(id).split(',').map(v => v.trim()).filter(Boolean);

  function businessIds(id) {
    const entries = list(id);
    if (entries.some(v => !/^[1-9]\d{0,14}$/.test(v))) throw new Error('Business IDs must be numbers separated by commas.');
    return entries.map(Number);
  }

  async function send(path, method, body) {
    const result = await apiFetch(BASE + path, { method, body: JSON.stringify(body) });
    if (!result) throw new Error('Not saved: your session may have ended.');
    return result;
  }

  // One submission at a time, so a double click cannot create two records.
  async function submitting(form, work) {
    if (form.dataset.busy) return;
    form.dataset.busy = '1';
    try { await work(); } catch (error) { toast(error.message, true); } finally { delete form.dataset.busy; }
  }

  function report(result, message) {
    const warnings = (result && result.warnings) || [];
    toast(warnings.length ? `${message} ${warnings.join(' ')}` : message, warnings.length > 0);
  }

  const openModal = id => { byId(id).classList.add('open'); };
  const closeModal = id => { byId(id).classList.remove('open'); };

  // ── Incident list ─────────────────────────────────────────────────────────
  async function loadIncidents(page) {
    const current = ticket('incidents');
    state.incidentsPage = page;
    const body = byId('sc-incidents');
    try {
      const data = await apiFetch(`${BASE}/incidents?${query({ status: value('sc-i-status'), severity: value('sc-i-severity'), page })}`);
      if (!data || !current()) return;
      state.incidents = data.incidents || [];
      pager('sc-incidents', page, data.has_more);
      if (!data.available) { body.innerHTML = emptyRow(7, data.problem || 'Unavailable.'); return; }
      body.innerHTML = state.incidents.length ? state.incidents.map(i => `<tr>
          <td style="color:var(--text-dim);font-size:11px;">${escHtml(fmtTime(i.detected_at))}</td>
          <td>${text(i.title)}</td><td>${badge(i.severity)}</td><td>${text(words(i.classification))}</td>
          <td>${text(words(i.status))}</td><td>${text(i.assigned_to)}</td>
          <td><button type="button" class="btn btn-ghost" data-incident="${escHtml(i.id)}">Open</button></td>
        </tr>`).join('') : emptyRow(7, 'No incidents match.');
      loadLinkTargets();
    } catch (error) {
      if (!current()) return;
      body.innerHTML = emptyRow(7, 'Unavailable — ' + error.message);
    }
  }

  // The "add selected events to" list: incidents that are still open.
  async function loadLinkTargets() {
    const current = ticket('targets');
    try {
      const data = await apiFetch(`${BASE}/incidents?page_size=100`);
      if (!data || !current()) return;
      const open = (data.incidents || []).filter(i => i.status !== 'resolved' && i.status !== 'false_positive');
      byId('sc-link-target').innerHTML = '<option value="">Choose an open incident</option>' +
        open.map(i => `<option value="${escHtml(i.id)}">#${escHtml(i.id)} ${escHtml(i.title)}</option>`).join('');
    } catch (error) {
      console.warn('[security] incident list for linking unavailable', error.message);
    }
  }

  // ── New incident ──────────────────────────────────────────────────────────
  function newIncident(eventIds) {
    state.linking = eventIds;
    byId('sc-new-form').reset();
    byId('sc-new-events').textContent = eventIds.length
      ? `${eventIds.length} selected event(s) will be linked.` : 'No events selected. You can link them later.';
    openModal('sc-new-modal');
    byId('sc-n-title').focus();
  }

  function createIncident(form) {
    return submitting(form, async () => {
      const classification = value('sc-n-class');
      if (classification === 'confirmed_breach' && !window.confirm('Record this as a confirmed breach?')) return;
      const result = await send('/incidents', 'POST', {
        title: value('sc-n-title'), severity: value('sc-n-severity'), classification,
        assigned_to: value('sc-n-assigned'), summary: value('sc-n-summary'),
        affected_business_ids: businessIds('sc-n-businesses'), affected_accounts: list('sc-n-accounts'),
        data_categories: list('sc-n-data'), event_ids: state.linking,
      });
      closeModal('sc-new-modal');
      sc.clearSelection();
      report(result, 'Incident created.');
      refresh();
      openIncident(result.incident.id);
    });
  }

  // ── Incident detail ───────────────────────────────────────────────────────
  async function openIncident(id) {
    const current = ticket('incident');
    try {
      const data = await apiFetch(`${BASE}/incidents/${encodeURIComponent(id)}`);
      if (!data || !current()) return;
      state.open = data.incident;
      const i = data.incident;
      byId('sc-inc-title').textContent = `Incident #${i.id}: ${i.title}`;
      byId('sc-inc-facts').innerHTML = [
        ['Status', words(i.status)], ['Detected', fmtTime(i.detected_at)], ['Created by', i.created_by],
        ['Resolved', i.resolved_at ? fmtTime(i.resolved_at) : null], ['Verification evidence', i.verification_evidence],
      ].map(([label, v]) => `<div><span style="color:var(--text-dim);">${escHtml(label)}:</span> ${text(v)}</div>`).join('');

      const fill = (field, v) => { byId(field).value = v === null || v === undefined ? '' : v; };
      byId('sc-edit-form').reset();
      fill('sc-e-title', i.title); fill('sc-e-severity', i.severity); fill('sc-e-class', i.classification);
      fill('sc-e-assigned', i.assigned_to); fill('sc-e-summary', i.summary);
      fill('sc-e-businesses', (i.affected_business_ids || []).join(', '));
      fill('sc-e-accounts', (i.affected_accounts || []).join(', '));
      fill('sc-e-data', (i.data_categories || []).join(', '));
      fill('sc-e-notify', i.notification_assessment);

      byId('sc-status-form').reset();
      byId('sc-s-status').innerHTML = (data.allowed_statuses || [])
        .map(s => `<option value="${escHtml(s)}">${escHtml(words(s))}</option>`).join('');
      showEvidence();
      byId('sc-note-form').reset();

      byId('sc-inc-notes').innerHTML = (data.notes || []).length ? data.notes.map(n => `<div style="margin-bottom:8px;">
          <span style="color:var(--text-dim);">${escHtml(fmtTime(n.created_at))} · ${escHtml(n.author)} · ${escHtml(words(n.kind))}</span>
          <div style="white-space:pre-wrap;">${escHtml(n.body)}</div></div>`).join('') : '<div class="empty">No timeline entries.</div>';
      byId('sc-inc-events').innerHTML = (data.events || []).length ? data.events.map(e => `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          ${badge(e.severity)} <span>#${escHtml(e.id)} ${escHtml(e.name)} · ${escHtml(e.occurrences)}× · ${escHtml(fmtTime(e.last_seen))} · ${text(e.username || e.ip)}</span>
          <button type="button" class="btn btn-ghost" data-unlink="${escHtml(e.id)}">Unlink</button></div>`).join('') +
          (data.more_events ? '<div class="empty">Only the newest 200 linked events are shown.</div>' : '')
        : '<div class="empty">No events linked.</div>';
      openModal('sc-inc-modal');
    } catch (error) {
      toast('Could not open the incident: ' + error.message, true);
    }
  }

  function showEvidence() {
    const resolving = byId('sc-s-status').value === 'resolved';
    byId('sc-s-evidence-row').style.display = resolving ? 'block' : 'none';
    byId('sc-s-evidence').required = resolving;
  }

  async function afterIncidentChange(result, message) {
    report(result, message);
    refresh();
    await openIncident(state.open.id);
  }

  function saveIncident(form) {
    return submitting(form, async () => {
      const classification = value('sc-e-class');
      if (classification === 'confirmed_breach' && state.open.classification !== 'confirmed_breach' &&
          !window.confirm('Record this as a confirmed breach?')) return;
      const result = await send(`/incidents/${state.open.id}`, 'PATCH', {
        title: value('sc-e-title'), severity: value('sc-e-severity'), classification,
        assigned_to: value('sc-e-assigned'), summary: value('sc-e-summary'),
        affected_business_ids: businessIds('sc-e-businesses'), affected_accounts: list('sc-e-accounts'),
        data_categories: list('sc-e-data'), notification_assessment: value('sc-e-notify'), reason: value('sc-e-reason'),
      });
      await afterIncidentChange(result, 'Incident updated.');
    });
  }

  function changeStatus(form) {
    return submitting(form, async () => {
      const status = value('sc-s-status');
      if ((status === 'resolved' || status === 'false_positive') &&
          !window.confirm(`Close this incident as ${words(status)}?`)) return;
      const body = { status, note: value('sc-s-note') };
      if (status === 'resolved') body.verification_evidence = value('sc-s-evidence');
      const result = await send(`/incidents/${state.open.id}/status`, 'POST', body);
      await afterIncidentChange(result, 'Status changed.');
    });
  }

  function addNote(form) {
    return submitting(form, async () => {
      const result = await send(`/incidents/${state.open.id}/notes`, 'POST', { kind: value('sc-t-kind'), body: value('sc-t-body') });
      await afterIncidentChange(result, 'Note added.');
    });
  }

  async function unlink(eventId) {
    const reason = (window.prompt('Why is this event being removed from the incident?') || '').trim();
    if (!reason) return;
    try {
      const result = await send(`/incidents/${state.open.id}/events/${encodeURIComponent(eventId)}/unlink`, 'POST', { reason });
      await afterIncidentChange(result, 'Event unlinked.');
    } catch (error) { toast(error.message, true); }
  }

  async function linkSelected() {
    const ids = sc.selectedEvents();
    const target = value('sc-link-target');
    if (!ids.length) { toast('Select one or more events first.', true); return; }
    if (!target) { toast('Choose an incident.', true); return; }
    try {
      const result = await send(`/incidents/${encodeURIComponent(target)}/events`, 'POST', { event_ids: ids });
      const skipped = result.requested - result.linked;
      toast(`Linked ${result.linked} event(s).${skipped ? ` ${skipped} already belong to an incident or are gone.` : ''}`, skipped > 0);
      sc.clearSelection();
      sc.reloadEvents();
    } catch (error) { toast(error.message, true); }
  }

  // ── Vulnerabilities ───────────────────────────────────────────────────────
  async function loadVulnerabilities(page) {
    const current = ticket('vulnerabilities');
    state.vulnPage = page;
    const body = byId('sc-vulns');
    try {
      const data = await apiFetch(`${BASE}/vulnerabilities?${query({ status: value('sc-v-fstatus'), severity: value('sc-v-fseverity'), page })}`);
      if (!data || !current()) return;
      state.vulns = data.vulnerabilities || [];
      pager('sc-vulns', page, data.has_more);
      if (!data.available) { body.innerHTML = emptyRow(7, data.problem || 'Unavailable.'); return; }
      body.innerHTML = state.vulns.length ? state.vulns.map((v, index) => `<tr>
          <td style="color:var(--text-dim);font-size:11px;">${escHtml(fmtTime(v.detected_at))}</td>
          <td>${text(v.title)}</td><td>${badge(v.severity)}</td><td>${text(v.component)}</td>
          <td>${text(words(v.status))}</td><td>${text(v.owner)}</td>
          <td><button type="button" class="btn btn-ghost" data-vuln="${index}">Open</button></td>
        </tr>`).join('') : emptyRow(7, 'No findings recorded. This does not mean none exist.');
    } catch (error) {
      if (!current()) return;
      body.innerHTML = emptyRow(7, 'Unavailable — ' + error.message);
    }
  }

  const VULN_FIELDS = { 'sc-v-title': 'title', 'sc-v-severity': 'severity', 'sc-v-component': 'component', 'sc-v-cve': 'cve',
    'sc-v-affected': 'affected_version', 'sc-v-fixed': 'fixed_version', 'sc-v-fix': 'recommendation', 'sc-v-owner': 'owner' };

  function openVulnerability(finding) {
    state.editing = finding;
    byId('sc-vuln-form').reset();
    byId('sc-vuln-title').textContent = finding ? `Finding #${finding.id}` : 'New Finding';
    Object.entries(VULN_FIELDS).forEach(([field, key]) => { if (finding) byId(field).value = finding[key] || ''; });
    byId('sc-v-source-row').style.display = finding ? 'none' : 'block';
    byId('sc-v-change').style.display = finding ? 'block' : 'none';
    byId('sc-v-reason').required = Boolean(finding);
    if (finding) byId('sc-v-status').value = finding.status;
    byId('sc-v-record').innerHTML = finding ? [
      ['Source', words(finding.source)], ['Detected', fmtTime(finding.detected_at)],
      ['Last verified', finding.last_verified_at ? fmtTime(finding.last_verified_at) : null],
      ['Evidence on record', finding.evidence],
    ].map(([label, v]) => `<div><span style="color:var(--text-dim);">${escHtml(label)}:</span> ${text(v)}</div>`).join('') : '';
    openModal('sc-vuln-modal');
    byId('sc-v-title').focus();
  }

  function saveVulnerability(form) {
    return submitting(form, async () => {
      const body = {};
      Object.entries(VULN_FIELDS).forEach(([field, key]) => { body[key] = value(field); });
      const finding = state.editing;
      if (!finding) {
        body.source = value('sc-v-source');
        await send('/vulnerabilities', 'POST', body);
      } else {
        body.status = value('sc-v-status');
        body.reason = value('sc-v-reason');
        // Left empty, the evidence on record is not touched.
        if (value('sc-v-evidence')) body.evidence = value('sc-v-evidence');
        await send(`/vulnerabilities/${encodeURIComponent(finding.id)}`, 'PATCH', body);
      }
      closeModal('sc-vuln-modal');
      toast(finding ? 'Finding updated.' : 'Finding recorded.');
      refresh();
    });
  }

  // ── Wiring ────────────────────────────────────────────────────────────────
  function refresh() {
    loadIncidents(state.incidentsPage);
    loadVulnerabilities(state.vulnPage);
    sc.reloadOverview();
  }

  function wire() {
    if (state.wired) return;
    state.wired = true;
    const on = (id, type, handler) => byId(id).addEventListener(type, handler);
    const onSubmit = (id, handler) => on(id, 'submit', e => { e.preventDefault(); handler(byId(id)); });
    on('sc-i-status', 'change', () => loadIncidents(1));
    on('sc-i-severity', 'change', () => loadIncidents(1));
    on('sc-incidents-prev', 'click', () => loadIncidents(Math.max(1, state.incidentsPage - 1)));
    on('sc-incidents-next', 'click', () => loadIncidents(state.incidentsPage + 1));
    on('sc-inc-new', 'click', () => newIncident([]));
    on('sc-incidents', 'click', e => { const b = e.target.closest('[data-incident]'); if (b) openIncident(b.dataset.incident); });
    on('sc-link', 'click', linkSelected);
    on('sc-link-new', 'click', () => {
      const ids = sc.selectedEvents();
      if (!ids.length) { toast('Select one or more events first.', true); return; }
      newIncident(ids);
    });
    onSubmit('sc-new-form', createIncident);
    onSubmit('sc-edit-form', saveIncident);
    onSubmit('sc-status-form', changeStatus);
    onSubmit('sc-note-form', addNote);
    on('sc-s-status', 'change', showEvidence);
    on('sc-inc-events', 'click', e => { const b = e.target.closest('[data-unlink]'); if (b) unlink(b.dataset.unlink); });
    on('sc-v-fstatus', 'change', () => loadVulnerabilities(1));
    on('sc-v-fseverity', 'change', () => loadVulnerabilities(1));
    on('sc-vulns-prev', 'click', () => loadVulnerabilities(Math.max(1, state.vulnPage - 1)));
    on('sc-vulns-next', 'click', () => loadVulnerabilities(state.vulnPage + 1));
    on('sc-vuln-new', 'click', () => openVulnerability(null));
    on('sc-vulns', 'click', e => { const b = e.target.closest('[data-vuln]'); if (b) openVulnerability(state.vulns[Number(b.dataset.vuln)]); });
    onSubmit('sc-vuln-form', saveVulnerability);
    ['sc-new-modal', 'sc-inc-modal', 'sc-vuln-modal'].forEach(id => {
      on(`${id}-close`, 'click', () => closeModal(id));
    });
    // Escape closes; a click outside does not, so half-written notes are not lost.
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') ['sc-new-modal', 'sc-inc-modal', 'sc-vuln-modal'].forEach(closeModal);
    });
  }

  window.loadSecurityCases = function () {
    wire();
    loadIncidents(1);
    loadVulnerabilities(1);
  };
})();
