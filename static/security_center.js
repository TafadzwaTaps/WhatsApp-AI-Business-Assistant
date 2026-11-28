/* Super Admin Security Center (security audit Stage 4b): overview, event explorer, audit trail.
   Only reads; incidents and findings are in security_cases.js. Uses apiFetch, escHtml, fmtTime, toast and exportDataset from dashboard.js.
   No inline handlers: everything is wired with addEventListener. */
(function () {
  'use strict';

  const SEVERITY_BADGE = { critical: 'badge-red', high: 'badge-red', medium: 'badge-amber', low: 'badge-blue', info: 'badge-green' };
  const state = { eventsPage: 1, auditPage: 1, events: [], selected: new Set(), wired: false, requests: {} };
  const AUDIT_PAGE_SIZE = 25;

  const byId = id => document.getElementById(id);
  const text = value => escHtml(value === null || value === undefined || value === '' ? '—' : value);
  // null means "could not be read": never shown as a zero.
  const figure = value => (value === null || value === undefined ? 'n/a' : String(value));

  function emptyRow(columns, message) {
    return `<tr><td colspan="${columns}"><div class="empty">${escHtml(message)}</div></td></tr>`;
  }

  // Only the newest request of each kind may draw, so a slow answer cannot overwrite a newer one.
  function badge(severity) {
    return `<span class="badge ${SEVERITY_BADGE[severity] || 'badge-blue'}">${escHtml(severity)}</span>`;
  }

  function showSelection() { byId('sc-selected').textContent = `${state.selected.size} selected`; }

  function ticket(kind) {
    const mine = (state.requests[kind] || 0) + 1;
    state.requests[kind] = mine;
    return () => state.requests[kind] === mine;
  }

  function query(params) {
    const search = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => { if (value !== '' && value !== null && value !== undefined) search.set(key, value); });
    return search.toString();
  }

  function card(label, value, sub) {
    return `<div class="stat-card purple"><div class="stat-label">${escHtml(label)}</div>
      <div class="stat-value">${escHtml(figure(value))}</div><div class="stat-sub">${escHtml(sub)}</div></div>`;
  }

  // ── Overview ──────────────────────────────────────────────────────────────
  async function loadOverview() {
    const current = ticket('overview');
    const days = byId('sc-days').value;
    try {
      const data = await apiFetch(`/admin/saas/security/overview?days=${encodeURIComponent(days)}`);
      if (!data || !current()) return;
      const ev = data.events || {};
      const counts = ev.counts || {};
      const sev = ev.by_severity || {};
      const incidents = data.open_incidents;
      const accounts = data.accounts_now;
      const period = Number(days) === 1 ? 'last 24 hours' : `last ${days} days`;

      let headline = 'Security events could not be read, so nothing below can be taken as "all clear".';
      if (ev.available) {
        const urgent = (sev.critical || 0) + (sev.high || 0);
        headline = urgent
          ? `${urgent} high or critical event${urgent === 1 ? '' : 's'} recorded in the ${period}: review them below.`
          : `No high or critical events recorded in the ${period}. This covers only what the application detects.`;
        if (ev.truncated) headline += ' Counts are incomplete: there are too many events for this period.';
      }
      byId('sc-headline').textContent = headline;

      byId('sc-cards').innerHTML = [
        card('Open critical incidents', incidents ? incidents.critical : null, 'right now'),
        card('Open high incidents', incidents ? incidents.high : null, 'right now'),
        card('Blocked requests', ev.blocked, `${period}, not all malicious`),
        card('Failed sign-ins', counts.failed_logins, period),
        card('Rate limit refusals', counts.rate_limited, period),
        card('Cross-business attempts', counts.cross_tenant, period),
        card('Webhook failures', counts.webhook_failures, period),
        card('AI blocks', counts.ai_blocks, 'prompt injection and quota'),
        card('Reused session tokens', counts.session_token_reuse, period),
        card('Blocked uploads', counts.blocked_uploads, period),
        card('Active IP restrictions', data.active_ip_restrictions, 'right now'),
        card('Open vulnerabilities', data.open_vulnerabilities, 'recorded in the register'),
      ].join('');

      const review = ev.to_review || [];
      byId('sc-review').innerHTML = !ev.available ? '' : review.length
        ? review.map(item => `<div><span class="badge ${SEVERITY_BADGE[item.severity] || 'badge-blue'}">${escHtml(item.severity)}</span>
            ${escHtml(item.name)}: <strong>${escHtml(item.occurrences)}</strong>
            <button type="button" class="btn btn-ghost" data-review="${escHtml(item.event_type)}">Show events</button></div>`).join('')
        : '';

      const m = data.monitoring || {};
      const lines = [
        `Event writer: ${m.writer_running ? 'running' : 'not running'} · ${figure(m.events_waiting)} waiting to be written`,
        `Newest event: ${ev.last_event_at ? fmtTime(ev.last_event_at) : 'none in this period'}`,
        ev.classified === false ? 'Newest event has no stored severity: check the database update for event recording.' : '',
        `Retention: ${m.retention_days > 0 ? `${Math.max(m.retention_days, 30)} days` : 'automatic deletion is off'}`,
        `Last security scan: ${m.last_security_scan ? fmtTime(m.last_security_scan) : 'no scan results are recorded'}`,
        accounts
          ? `Right now: ${accounts.locked_accounts} locked accounts · ${accounts.flagged_ips} flagged addresses · ${accounts.open_risk_flags} open risk flags`
          : 'Account status is unavailable.',
      ];
      byId('sc-monitoring').innerHTML = lines.filter(Boolean).map(line => `<div>${escHtml(line)}</div>`).join('');

      const types = byId('sc-f-type');
      if (types.options.length <= 1) {
        (data.catalog || []).forEach(entry => types.add(new Option(entry.name, entry.event_type)));
      }
    } catch (error) {
      if (!current()) return;
      byId('sc-headline').textContent = 'Security overview unavailable — ' + error.message;
      byId('sc-cards').innerHTML = '';
      byId('sc-review').innerHTML = '';
      byId('sc-monitoring').innerHTML = '';
    }
  }

  // ── Event explorer ────────────────────────────────────────────────────────
  function eventFilters() {
    const value = id => byId(id).value.trim();
    return {
      q: value('sc-f-search'), severity: value('sc-f-severity'), event_type: value('sc-f-type'),
      business_id: value('sc-f-business'), username: value('sc-f-user'), endpoint: value('sc-f-endpoint'),
      ip: value('sc-f-ip'), incident_id: value('sc-f-incident'),
      date_from: value('sc-f-from'), date_to: value('sc-f-to'),
    };
  }

  function pager(prefix, page, hasMore) {
    byId(`${prefix}-page`).textContent = `Page ${page}`;
    byId(`${prefix}-prev`).disabled = page <= 1;
    byId(`${prefix}-next`).disabled = !hasMore;
  }

  async function loadEvents(page) {
    const current = ticket('events');
    state.eventsPage = page;
    const body = byId('sc-events');
    body.innerHTML = emptyRow(8, 'Loading…');
    try {
      const data = await apiFetch(`/admin/saas/security/events?${query({ ...eventFilters(), page })}`);
      if (!data || !current()) return;
      state.events = data.events || [];
      pager('sc-events', page, data.has_more);
      if (!data.available) { body.innerHTML = emptyRow(8, data.problem || 'Unavailable.'); return; }
      body.innerHTML = state.events.length ? state.events.map((e, index) => `<tr>
          <td>${e.incident_id ? '#' + escHtml(e.incident_id) : `<input type="checkbox" data-select="${escHtml(e.id)}" aria-label="Select event ${escHtml(e.id)}" style="width:auto;"${state.selected.has(Number(e.id)) ? ' checked' : ''}/>`}</td>
          <td style="color:var(--text-dim);font-size:11px;">${escHtml(fmtTime(e.last_seen))}</td>
          <td>${escHtml(e.name)}</td>
          <td><span class="badge ${SEVERITY_BADGE[e.severity] || 'badge-blue'}">${escHtml(e.severity)}</span></td>
          <td>${text(e.username ? '@' + e.username : e.ip)}</td>
          <td>${e.business_id === null || e.business_id === undefined ? '—' : '#' + escHtml(e.business_id)}</td>
          <td>${escHtml(e.occurrences)}</td>
          <td><button type="button" class="btn btn-ghost" data-event="${index}">Details</button></td>
        </tr>`).join('') : emptyRow(8, 'No events match these filters.');
    } catch (error) {
      if (!current()) return;
      pager('sc-events', page, false);
      body.innerHTML = emptyRow(8, 'Unavailable — ' + error.message);
    }
  }

  function showEvent(event) {
    const rows = [
      ['Event', event.name], ['Type', event.event_type], ['Severity', event.severity], ['Outcome', event.outcome],
      ['First seen', fmtTime(event.first_seen)], ['Last seen', fmtTime(event.last_seen)], ['Occurrences', event.occurrences],
      ['Address', event.ip], ['Account', event.username], ['Business', event.business_id], ['Route', event.endpoint],
      ['Detection rule', event.rule], ['Browser or client', event.user_agent], ['Incident', event.incident_id],
      ['Why it was recorded', event.why], ['Action taken', event.action_taken], ['What to check', event.follow_up],
    ];
    byId('sc-detail-body').innerHTML = rows.map(([label, value]) =>
      `<div><span style="color:var(--text-dim);">${escHtml(label)}:</span> ${text(value)}</div>`).join('');
    byId('sc-detail-json').textContent = JSON.stringify(event.details || {}, null, 2);
    const block = byId('sc-detail-block');
    block.dataset.ip = event.ip || '';
    block.style.display = event.ip ? '' : 'none';
    byId('sc-detail-modal').classList.add('open');
    byId('sc-detail-close').focus();
  }

  function closeEvent() { byId('sc-detail-modal').classList.remove('open'); }

  // ── Audit trail ───────────────────────────────────────────────────────────
  async function loadAudit(page) {
    const current = ticket('audit');
    state.auditPage = page;
    const body = byId('sc-audit');
    body.innerHTML = emptyRow(7, 'Loading…');
    const value = id => byId(id).value.trim();
    try {
      const data = await apiFetch(`/admin/saas/audit-logs?${query({
        actor: value('sc-a-actor'), action: value('sc-a-action'), business_id: value('sc-a-business'),
        date_from: value('sc-a-from'), date_to: value('sc-a-to'),
        limit: AUDIT_PAGE_SIZE, offset: (page - 1) * AUDIT_PAGE_SIZE,
      })}`);
      if (!data || !current()) return;
      const logs = data.logs || [];
      pager('sc-audit', page, data.has_more);
      body.innerHTML = logs.length ? logs.map(l => `<tr>
          <td style="color:var(--text-dim);font-size:11px;">${escHtml(fmtTime(l.created_at))}</td>
          <td>${text(l.actor_username)}</td>
          <td><span class="badge badge-purple">${escHtml(l.action || '')}</span>${l.outcome ? ' ' + escHtml(l.outcome) : ''}</td>
          <td>${text([l.target_type, l.target_id].filter(Boolean).join(' '))}</td>
          <td>${l.business_id === null || l.business_id === undefined ? '—' : '#' + escHtml(l.business_id)}</td>
          <td style="color:var(--text-dim);">${text(l.reason)}</td>
          <td style="color:var(--text-dim);font-size:11px;">${text(l.ip_address)}</td>
        </tr>`).join('') : emptyRow(7, 'No audit entries to show. An unreadable log also appears empty.');
    } catch (error) {
      if (!current()) return;
      pager('sc-audit', page, false);
      body.innerHTML = emptyRow(7, 'Unavailable — ' + error.message);
    }
  }

  // ── Wiring ────────────────────────────────────────────────────────────────
  function wire() {
    if (state.wired) return;
    state.wired = true;
    const on = (id, type, handler) => byId(id).addEventListener(type, handler);
    on('sc-days', 'change', loadOverview);
    on('sc-refresh', 'click', loadSecurityCenter);
    on('sc-event-filters', 'submit', e => { e.preventDefault(); loadEvents(1); });
    on('sc-event-filters', 'reset', () => setTimeout(() => loadEvents(1), 0));
    on('sc-events-prev', 'click', () => loadEvents(Math.max(1, state.eventsPage - 1)));
    on('sc-events-next', 'click', () => loadEvents(state.eventsPage + 1));
    on('sc-export', 'click', () => exportDataset('csv', 'security_events'));
    on('sc-events', 'click', e => {
      const button = e.target.closest('[data-event]');
      if (button) showEvent(state.events[Number(button.dataset.event)]);
    });
    on('sc-events', 'change', e => {
      const box = e.target.closest('[data-select]');
      if (!box) return;
      if (box.checked) state.selected.add(Number(box.dataset.select)); else state.selected.delete(Number(box.dataset.select));
      showSelection();
    });
    on('sc-review', 'click', e => {
      const button = e.target.closest('[data-review]');
      if (!button) return;
      byId('sc-event-filters').reset();
      byId('sc-f-type').value = button.dataset.review;
      setTimeout(() => { loadEvents(1); byId('sc-event-filters').scrollIntoView({ block: 'start' }); }, 0);
    });
    on('sc-audit-filters', 'submit', e => { e.preventDefault(); loadAudit(1); });
    on('sc-audit-filters', 'reset', () => setTimeout(() => loadAudit(1), 0));
    on('sc-audit-prev', 'click', () => loadAudit(Math.max(1, state.auditPage - 1)));
    on('sc-audit-next', 'click', () => loadAudit(state.auditPage + 1));
    on('sc-detail-close', 'click', closeEvent);
    on('sc-detail-modal', 'click', e => { if (e.target === byId('sc-detail-modal')) closeEvent(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeEvent(); });
  }

  function loadSecurityCenter() {
    wire();
    loadOverview();
    loadEvents(1);
    loadAudit(1);
    if (window.loadSecurityCases) window.loadSecurityCases();
    if (window.loadSecurityContainment) window.loadSecurityContainment();
  }

  window.SecurityCenter = {
    byId, text, query, ticket, pager, emptyRow, badge,
    selectedEvents: () => [...state.selected],
    clearSelection: () => { state.selected.clear(); showSelection(); loadEvents(state.eventsPage); },
    reloadEvents: () => loadEvents(state.eventsPage),
    reloadOverview: () => { loadOverview(); loadAudit(state.auditPage); },
  };
  window.loadSecurityCenter = loadSecurityCenter;
})();
