/* build: 2026-07-06 09:13 UTC — domain: wazibothq.com */
const API = 'https://wazibothq.com';
const ROUTES = {
  login:         '/auth/login',
  register:      '/auth/signup',
  refresh:       '/auth/refresh',
  adminStats:    '/admin/stats',
  adminBiz:      '/admin/businesses',
  products:      '/products',
  orders:        '/orders',
  conversations: '/chat/conversations',
  customers:     '/customers',
  broadcast:     '/broadcast',
  // Phase 1-7 additions
  crmSegments:   '/crm/segments',
  crmInactive:   '/crm/inactive',
  campaigns:     '/campaigns/send',
  campaignPrev:  '/campaigns/preview',
  campaignAuds:  '/campaigns/audiences',
  templates:     '/templates',
  reminders:     '/payments/reminders/pending',
  remindersSend: '/payments/reminders/send',
  analyticsStats:'/analytics/stats',
  analyticsTop:  '/analytics/top-customers',
  analyticsInsights: '/analytics/conversation-insights',
  // SuperAdmin 2.0 — Platform Control Center
  saasTenant:    '/admin/saas/tenants',     // + /{id}
  auditLogs:     '/admin/saas/audit-logs',
  riskFlags:     '/admin/saas/risk-flags',
  abuseScan:     '/admin/saas/abuse/scan',
  usageMessages: '/admin/saas/usage/messages',
  usageAI:       '/admin/saas/usage/ai',
  usageLimits:   '/admin/saas/usage/limits',
  usageCampaigns: '/admin/saas/usage/campaigns',
  security:      '/admin/saas/security',
  platformControls:    '/admin/saas/platform/controls',
  platformMaintenance: '/admin/saas/platform/maintenance',
  featureFlags:        '/admin/saas/flags',
  cohorts:             '/admin/saas/cohorts',
  churn:               '/admin/saas/churn',
};

let token       = localStorage.getItem('wazi_token');
let refreshTok  = localStorage.getItem('wazi_refresh');
let userRole    = localStorage.getItem('wazi_role');
let userName    = localStorage.getItem('wazi_user');
let bizName     = localStorage.getItem('wazi_biz');
let bizId       = parseInt(localStorage.getItem('wazi_business_id') || '0', 10);

// ── STARTUP: validate stored token before any API calls fire ──
// Decode the JWT expiry without a library (base64 decode the payload).
// If the access token is already expired on page load, clear it immediately
// so the login screen shows rather than firing authenticated requests.
(function() {
  if (!token) return;
  try {
    const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g,'+').replace(/_/g,'/')));
    const expiredAt = payload.exp * 1000;
    if (Date.now() >= expiredAt) {
      // Access token expired — clear it so the login screen shows.
      // Keep the refresh token so tryRefresh() can silently re-authenticate
      // if the user had a valid refresh token.
      token = null;
      localStorage.removeItem('wazi_token');
    }
  } catch (_) {
    // Token can't be decoded — treat as expired, clear access token only.
    // Keep refresh token so silent refresh can try to recover the session.
    token = null;
    localStorage.removeItem('wazi_token');
  }
})();
let activePhone = null;
let customerPhones = [];
let _crmTableData  = [];   // cache of /crm/segments/all rows for safe View button lookups

// ── MOBILE SIDEBAR TOGGLE ─────────────────────────────────
function toggleSidebar() {
  const sidebar = document.querySelector('.sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  sidebar.classList.toggle('open');
  overlay.classList.toggle('open');
}

function closeSidebar() {
  const sidebar = document.querySelector('.sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  sidebar.classList.remove('open');
  overlay.classList.remove('open');
}

// ── DESKTOP SIDEBAR COLLAPSE ───────────────────────────────
function sidebarToggleCollapse() {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) return;
  const collapsed = sidebar.classList.toggle('collapsed');
  try { localStorage.setItem('wazi_sidebar_collapsed', collapsed ? '1' : '0'); } catch (_) {}
}

// Restore collapsed state on load
(function() {
  try {
    if (localStorage.getItem('wazi_sidebar_collapsed') === '1') {
      document.addEventListener('DOMContentLoaded', () => {
        const sidebar = document.querySelector('.sidebar');
        if (sidebar) sidebar.classList.add('collapsed');
      });
    }
  } catch (_) {}
})();

// ── SIDEBAR SEARCH ─────────────────────────────────────────
function sidebarSearch(query) {
  const nav = document.getElementById('sidebar-nav');
  if (!nav) return;
  const q = (query || '').trim().toLowerCase();
  const sections = nav.querySelectorAll('.nav-section');

  nav.querySelectorAll('.nav-item').forEach(item => {
    const text = item.textContent.toLowerCase();
    item.style.display = (!q || text.includes(q)) ? '' : 'none';
  });

  // Hide section headers whose items are all filtered out
  sections.forEach(section => {
    let el = section.nextElementSibling;
    let hasVisible = false;
    while (el && !el.classList.contains('nav-section')) {
      if (el.classList.contains('nav-item') && el.style.display !== 'none') {
        hasVisible = true;
        break;
      }
      el = el.nextElementSibling;
    }
    section.style.display = (!q || hasVisible) ? '' : 'none';
  });
}

// ── AUTH ──────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.login-tab').forEach((t,i) => t.classList.toggle('active', (i===0&&tab==='login')||(i===1&&tab==='register')));
  document.getElementById('tab-login').classList.toggle('active', tab==='login');
  document.getElementById('tab-register').classList.toggle('active', tab==='register');
  const _lerr=document.getElementById('login-error'); if(_lerr) _lerr.textContent='';
}

async function doLogin() {
  // Use requestAnimationFrame to ensure browser autofill has completed
  // before reading field values — fixes Chrome desktop autofill race condition
  await new Promise(r => requestAnimationFrame(r));

  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  const errEl    = document.getElementById('login-error');
  const btnEl    = document.querySelector('.btn-login');
  errEl.textContent = '';

  if (!username || !password) { errEl.textContent = 'Enter username and password'; return; }

  if (btnEl) { btnEl.disabled = true; btnEl.textContent = 'Signing in…'; }
  try {
    const res = await fetch(API + ROUTES.login, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ username, password })
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      errEl.textContent = d.detail || 'Login failed. Check your username and password.';
      return;
    }
    const data = await res.json();
    saveSession(data, username);
    const _ls = document.getElementById('login-screen');
    if (_ls) _ls.style.display = 'none';
    const _sb0 = document.getElementById('sidebar');
    const _sbt0 = document.getElementById('sidebar-toggle-btn');
    if (_sb0)  _sb0.style.display  = '';
    if (_sbt0) _sbt0.style.display = '';
    if (window.location.pathname !== '/dashboard') {
      window.location.href = '/dashboard';
      return;
    }
    init();
  } catch(e) {
    errEl.textContent = 'Cannot reach server. Check your connection.';
  } finally {
    if (btnEl) { btnEl.disabled = false; btnEl.textContent = 'Sign In →'; }
  }
}

async function doRegister() {
  const errEl = document.getElementById('login-error');
  errEl.textContent = '';
  const payload = {
    business_name:     document.getElementById('reg-bizname').value.trim(),
    username:          document.getElementById('reg-username').value.trim(),
    password:          document.getElementById('reg-password').value,
    confirm_password:  document.getElementById('reg-confirm').value,
    category:          document.getElementById('reg-category')?.value || undefined,
    use_shared_number: true,   // shared number — no Meta setup needed
  };
  if (!payload.business_name || !payload.username || !payload.password) {
    errEl.textContent = 'All fields are required'; return;
  }
  try {
    const res = await fetch(API + ROUTES.register, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      const d = await res.json();
      if (Array.isArray(d.detail)) {
        errEl.textContent = d.detail.map(e => e.msg).join(' • ');
      } else {
        errEl.textContent = d.detail || 'Registration failed';
      }
      return;
    }
    const data = await res.json();
    saveSession(data, payload.username);
    const _ls2=document.getElementById('login-screen'); if(_ls2) _ls2.style.display='none';
    // Show sidebar elements now that user is logged in
    const _sb = document.getElementById('sidebar');
    const _sbt = document.getElementById('sidebar-toggle-btn');
    if (_sb)  _sb.style.display  = '';
    if (_sbt) _sbt.style.display = '';
    init();
  } catch(e) { errEl.textContent = 'Cannot reach server'; }
}

function saveSession(data, username) {
  // FIX: use ||= pattern so partial refresh responses don't clear existing values
  token      = data.access_token  || token;
  refreshTok = data.refresh_token || refreshTok;   // preserve across partial responses
  userRole   = data.role          || userRole;
  userName   = username           || userName;
  bizName    = data.business_name || bizName || '';
  // Persist username so inbox.js can show "Agent Name is replying" in handoff banner
  if (userName) localStorage.setItem('wazibot_username', userName);
  // FIX: always persist business_id — needed for WebSocket auth and chat/send
  if (data.business_id) {
    bizId = data.business_id;
    localStorage.setItem('wazi_business_id', bizId);
  }
  localStorage.setItem('wazi_token',   token      || '');
  localStorage.setItem('wazi_refresh', refreshTok || '');
  localStorage.setItem('wazi_role',    userRole   || '');
  localStorage.setItem('wazi_user',    userName   || '');
  localStorage.setItem('wazi_biz',     bizName    || '');
  // H4: if signup included a pre-selected plan, store for checkout redirect
  if (data.selected_tier) {
    localStorage.setItem('wazi_pending_tier',    data.selected_tier);
    localStorage.setItem('wazi_pending_period',  data.billing_period || 'monthly');
  }
}

let _refreshInFlight = false;
async function tryRefresh() {
  // If there was never a refresh token, user is simply not logged in — don't redirect
  if (!refreshTok) return false;
  // Prevent concurrent refresh attempts
  if (_refreshInFlight) {
    // Wait up to 3s for the in-flight refresh to complete
    await new Promise(r => setTimeout(r, 3000));
    return !!token;
  }
  _refreshInFlight = true;
  try {
    const res = await fetch(API + ROUTES.refresh, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshTok })
    });
    if (!res.ok) {
      // 401/403 from refresh = token genuinely expired → must re-login
      logout();
      return false;
    }
    const data = await res.json();
    saveSession(data, userName);
    return true;
  } catch {
    // Network error — don't logout, just fail this request silently
    // User can retry; we don't force them back to login on a blip
    return false;
  } finally {
    _refreshInFlight = false;
  }
}

function logout() {
  // FIX: clear all session state then do a hard redirect so no stale
  // in-memory variables survive into the next session.
  ['wazi_token','wazi_refresh','wazi_role','wazi_user','wazi_biz','wazi_business_id']
    .forEach(k => localStorage.removeItem(k));
  token = refreshTok = userRole = userName = bizName = null;
  bizId = 0;
  window.location.href = '/';
}

/* Sprint 4 — Client-side /me cache (60s TTL)
   /me is called 12+ times per page load across functions.
   This cache reduces those to 1 real HTTP request per minute.
   Cache is per-session (memory only), invalidated on page reload. */
const _meCache = { data: null, ts: 0, ttl: 60000 };

// Global currency symbol — every dashboard money display reads this instead
// of hardcoding '$'. Kept in sync with the business's saved currency_symbol
// every time /me is fetched (see getCachedMe below). Defaults to '$' so
// nothing breaks before the first /me call resolves.
window.CURRENT_CURRENCY_SYMBOL = '$';
function getCurrencySymbol() { return window.CURRENT_CURRENCY_SYMBOL || '$'; }

// ── Business type helpers (service vs product) ────────────────────────────────
const _SERVICE_CATEGORIES = new Set([
  'Salon','Barbershop','Beauty Spa','Fitness','Gym',
  'Automotive','Transport','Logistics','Courier','Education',
  'Tutoring','Consulting','Marketing Agency','Freelancer',
  'Professional Services','Photography','Event Planning',
  'Hotel','Guest House','Airbnb','Travel Agency',
  'Clinic','Hospital','Doctor','Dentist','Pharmacy','Repair Services',
]);

async function onBizTypeToggle(isService, _skipSave) {
  // Animate the custom toggle track and thumb
  const track = document.getElementById('biz-type-track');
  const thumb = document.getElementById('biz-type-thumb');
  if (track) track.style.background = isService ? 'var(--green)' : 'var(--border)';
  if (thumb) thumb.style.transform  = isService ? 'translateX(20px)' : 'translateX(0)';
  _setServiceMode(isService);

  // Auto-save immediately — this toggle changes core product-page behaviour
  // (stock tracking, Out of Stock) and users expect a switch to take effect
  // the moment they flip it, not after finding a separate Save button.
  // _skipSave=true is used when this function is called just to sync the UI
  // from a fresh /me read (no need to re-save what we just loaded).
  if (_skipSave) return;

  const chk = document.getElementById('set-is-service-business');
  try {
    await apiFetch('/me', {
      method: 'PATCH',
      body: JSON.stringify({ is_service_business: isService }),
    });
    invalidateMeCache();

    // Verify the save actually took effect — a 200 OK response does not
    // guarantee persistence (e.g. a backend model silently dropping an
    // unrecognised field looks identical to success). Re-read fresh and
    // confirm the DB agrees before telling the user it worked.
    const fresh = await apiFetch('/me');
    if (fresh && !!fresh.is_service_business !== isService) {
      if (chk) chk.checked = !!fresh.is_service_business;
      const trackR = document.getElementById('biz-type-track');
      const thumbR = document.getElementById('biz-type-thumb');
      const actual = !!fresh.is_service_business;
      if (trackR) trackR.style.background = actual ? 'var(--green)' : 'var(--border)';
      if (thumbR) thumbR.style.transform  = actual ? 'translateX(20px)' : 'translateX(0)';
      _setServiceMode(actual);
      toast('⚠️ Business type did not save — please try again or contact support', true);
      return;
    }

    toast(isService ? '✅ Switched to Services mode' : '✅ Switched to Products mode');
  } catch (e) {
    // Save failed — revert the toggle and UI to reflect reality, and tell the user why
    if (chk) chk.checked = !isService;
    if (track) track.style.background = !isService ? 'var(--green)' : 'var(--border)';
    if (thumb) thumb.style.transform  = !isService ? 'translateX(20px)' : 'translateX(0)';
    _setServiceMode(!isService);
    toast('Failed to save business type: ' + e.message, true);
  }
}

function _setServiceMode(isService) {
  window.IS_SERVICE_BUSINESS = !!isService;

  // Update description text under the toggle
  const desc = document.getElementById('biz-type-desc');
  if (desc) desc.textContent = isService
    ? 'Services mode — stock tracking hidden. Items always shown as available.'
    : 'Products mode — stock tracking and "Out of Stock" are enabled.';

  // Highlight active side label
  const lp = document.getElementById('biz-type-lbl-product');
  const ls = document.getElementById('biz-type-lbl-service');
  if (lp) lp.style.color = isService ? 'var(--text-dim)' : 'var(--text)';
  if (ls) ls.style.color = isService ? 'var(--text)'    : 'var(--text-dim)';

  _populateOrderStatusFilter();

  _applyServiceMode(isService);
}

function _applyServiceMode(isService) {
  // Add-product form: show/hide stock note
  const note   = document.getElementById('service-stock-note');
  const optLbl = document.getElementById('stock-optional-label');
  if (note)   note.style.display = isService ? 'block' : 'none';
  if (optLbl) optLbl.textContent = isService ? '(not needed for services)' : '(optional)';

  // Edit modal: hide Out of Stock option for service businesses
  document.querySelectorAll('.edit-stock-option').forEach(o => {
    o.style.display = isService ? 'none' : '';
  });
  if (isService) {
    const sel = document.getElementById('edit-prod-status');
    if (sel && sel.value === 'out_of_stock') sel.value = 'active';
  }
}

function _autoDetectServiceMode(category) {
  return !!category && _SERVICE_CATEGORIES.has(category);
}

function _updateCurrencyLabels(sym) {
  sym = sym || getCurrencySymbol();
  const pl = document.getElementById('product-price-label');
  if (pl) pl.textContent = 'Price (' + sym + ')';
  const rl = document.getElementById('stat-revenue-currency-label');
  if (rl) rl.textContent = 'Total ' + sym;
  const dl = document.getElementById('delivery-fee-label');
  if (dl) dl.textContent = 'Delivery Fee (' + sym + ')';
}



async function getCachedMe() {
  const now = Date.now();
  if (_meCache.data && (now - _meCache.ts) < _meCache.ttl) {
    return _meCache.data;
  }
  try {
    const result = await apiFetch('/me');
    if (result) {
      _meCache.data = result;
      _meCache.ts   = now;
      if (result.currency_symbol) {
        window.CURRENT_CURRENCY_SYMBOL = result.currency_symbol;
        _updateCurrencyLabels(result.currency_symbol);
      }
      // Restore service mode from DB; fall back to category auto-detect
      const _isSvc = result.is_service_business != null
        ? !!result.is_service_business
        : _autoDetectServiceMode(result.category);
      window.IS_SERVICE_BUSINESS = _isSvc;
      _setServiceMode(_isSvc);
      const _chk0 = document.getElementById('set-is-service-business');
      if (_chk0) { _chk0.checked = _isSvc; onBizTypeToggle(_isSvc, /*_skipSave=*/true); }
    }
    return result;
  } catch (e) {
    return _meCache.data || null;
  }
}

function invalidateMeCache() {
  _meCache.data = null;
  _meCache.ts   = 0;
}

// ── API ───────────────────────────────────────────────────
async function apiFetch(path, opts={}, _retried=false) {
  // Guard: never fire API calls without a token — avoids 401 spam on page load
  if (!token && !opts._public) return null;
  // FIX: _retried flag prevents infinite refresh loops — one retry max.
  // If the refreshed token also gets a 401, logout() is called once.

  // Merge headers properly — object spread (...opts) would otherwise REPLACE
  // the entire headers object if opts.headers is set, silently dropping
  // the Authorization header on any call that passes its own Content-Type
  // (e.g. POST/PUT requests with a JSON body).
  // Fix 2: do NOT set Content-Type for FormData — browser must set it with
  // the correct multipart boundary for file uploads (CSV import, logo upload).
  // For all other bodies, default to application/json.
  const isFormData = opts.body instanceof FormData;
  const mergedHeaders = {
    ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
    'Authorization': `Bearer ${token}`,
    ...(opts.headers || {}),
  };
  const finalOpts = { ...opts, headers: mergedHeaders };

  try {
    const res = await fetch(API + path, finalOpts);
    if (res.status === 401 && !_retried) {
      let detail = '';
      try { const e = await res.json(); detail = e.detail || ''; } catch {}
      console.warn(
        '[apiFetch] 401 — attempting token refresh',
        '\n  path:', path,
        '\n  method:', finalOpts.method || 'GET',
        '\n  hasAuthHeader:', !!mergedHeaders.Authorization,
        '\n  tokenPreview:', token ? token.slice(0, 12) + '…' : '(none)',
        '\n  serverDetail:', detail || '(none)',
      );
      const refreshed = await tryRefresh();
      if (refreshed) return apiFetch(path, opts, true);  // one retry only
      // Refresh failed — show UI feedback if we have a status element
      console.error('[apiFetch] 401 after refresh — session expired', path);
      const statusEl = document.getElementById('api-status-text');
      if (statusEl) statusEl.textContent = 'Session expired — logging out…';
      return null;
    }
    if (res.status === 403) {
      // Fix 3: distinguish plan-limit 403 from auth 403.
      // plan_required errors show an upgrade modal and do NOT logout.
      // All other 403s (wrong role, cross-tenant) still throw normally.
      let body403 = {};
      try { body403 = await res.json(); } catch {}
      const detail = body403.detail || body403;
      if (detail && (detail.error === 'plan_required' || (typeof detail === 'string' && detail.includes('plan_required')))) {
        showPlanUpgradeModal(detail);
        return null;  // caller receives null — same as auth failure, no logout
      }
      // Not a plan error — throw normally so caller handles it
      const msg403 = (typeof detail === 'string' ? detail : detail.message) || res.statusText || 'Forbidden';
      console.error('[apiFetch] 403', path, msg403);
      throw new Error(msg403);
    }
    if (!res.ok) {
      let msg = res.statusText || 'Request failed';
      try { const e = await res.json(); msg = e.detail || msg; } catch {}
      console.error('[apiFetch] error', path, res.status, msg);
      throw new Error(msg);
    }
    return res.json();
  } catch (err) {
    if (err instanceof TypeError) throw new Error('Cannot reach server — is the backend running?');
    throw err;
  }
}

// ── SIDEBAR ───────────────────────────────────────────────
function buildSidebar() {
  const _fu=document.getElementById('footer-user'); if(_fu) _fu.textContent=userName||'';
  const nav = document.getElementById('sidebar-nav');
  if (userRole === 'superadmin') {
    const _rl=document.getElementById('sidebar-role-label'); if(_rl) _rl.textContent='Super Admin';
    const _rb=document.getElementById('sidebar-role-badge'); if(_rb) _rb.innerHTML='<span class="badge badge-purple">SUPERADMIN</span>';
    nav.innerHTML = `
      <div class="nav-section">Platform</div>
      <button class="nav-item admin-item active" onclick="showSection('admin-overview',this);closeSidebar()"><span class="icon">🌐</span> Overview <span class="status-dot"></span></button>
      <button class="nav-item admin-item" onclick="showSection('admin-businesses',this);closeSidebar()"><span class="icon">🏢</span> Businesses</button>
      <button class="nav-item admin-item" onclick="showSection('admin-audit',this);closeSidebar()"><span class="icon">🛡️</span> Audit & Abuse</button>
      <button class="nav-item admin-item" onclick="showSection('admin-controls',this);closeSidebar()"><span class="icon">🚨</span> Platform Controls</button>
      <button class="nav-item admin-item" onclick="showSection('admin-cohorts',this);closeSidebar()"><span class="icon">📈</span> Cohorts & Churn</button>`;
  } else {
    const _rl2=document.getElementById('sidebar-role-label'); if(_rl2) _rl2.textContent=bizName||'Business';
    const _rb2=document.getElementById('sidebar-role-badge'); if(_rb2) _rb2.innerHTML='<span class="badge badge-green">BUSINESS</span>';
    if(bizName){const _bnh=document.getElementById('biz-name-header');if(_bnh)_bnh.textContent='🟢 '+bizName;}
    nav.innerHTML = `
      <div class="nav-section">Dashboard</div>
      <button class="nav-item active" onclick="showSection('overview',this);closeSidebar()"><span class="icon">📊</span> Overview <span class="status-dot"></span></button>
      <button class="nav-item" onclick="showSection('orders',this);closeSidebar()"><span class="icon">🛒</span> Orders</button>
      <button class="nav-item" onclick="showSection('products',this);closeSidebar()"><span class="icon">📦</span> Products</button>
      <button class="nav-item" onclick="showSection('crm',this);closeSidebar()"><span class="icon">👥</span> Customers <span id="nav-crm-badge" class="nav-badge" style="display:none"></span></button>
      <button class="nav-item" data-gated="true" onclick="_navGuard('reminders',this);closeSidebar()"><span class="icon">⏳</span> Reminders <span id="nav-rem-badge" class="nav-badge nav-badge-amber" style="display:none"></span></button>
      <button class="nav-item" data-gated="true" onclick="_navGuard('conversations',this);closeSidebar()"><span class="icon">💬</span> Conversations</button>
      <button class="nav-item" data-gated="true" onclick="_navGuard('__inbox__',this);closeSidebar()"><span class="icon">📥</span> Live Inbox</button>
      <button class="nav-item" data-gated="true" onclick="_navGuard('handoff',this);closeSidebar()"><span class="icon">👤</span> Handoff <span id="nav-handoff-badge" class="nav-badge nav-badge-red" style="display:none"></span></button>
      <button class="nav-item" data-gated="true" onclick="_navGuard('growth-automation',this);closeSidebar()"><span class="icon">🚀</span> Growth</button>
      <button class="nav-item" data-gated="true" onclick="_navGuard('broadcast',this);closeSidebar()"><span class="icon">📢</span> Campaigns</button>
      <button class="nav-item" onclick="showSection('bookings',this);closeSidebar()"><span class="icon">🗓️</span> Bookings</button>
      <button class="nav-item" onclick="showSection('settings',this);closeSidebar()"><span class="icon">⚙️</span> Settings</button>
      <button class="nav-item" onclick="showSection('marketing-kit',this);loadMarketingKit();closeSidebar()"><span class="icon">📣</span> Marketing Kit</button>
      <div class="nav-section">Growth</div>
      <button class="nav-item" onclick="showSection('settings',this);switchSettingsTab('referrals',null);loadReferralTab();closeSidebar()"><span class="icon">🔗</span> Referrals <span id="nav-ref-badge" class="nav-badge nav-badge-green" style="display:none"></span></button>`;
  }
}

// Guards restricted nav items (Reminders, Conversations, Live Inbox, Handoff,
// Growth, Campaigns) once the trial's grace period has passed. Uses the
// already-cached /trial/status result (loadTrialBanner keeps this fresh)
// so this adds no extra network round-trip on every click.
function _navGuard(name, btn) {
  if (_trialCache && _trialCache.restricted) {
    toast('🔒 This feature is restricted — your trial grace period has ended. Upgrade to unlock it.', true);
    window.location.href = '/static/pricing.html';
    return;
  }
  if (name === '__inbox__') {
    window.open('/inbox', '_blank');
    return;
  }
  showSection(name, btn);
}

function showSection(name, btn) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('section-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
  if (name==='admin-overview'||name==='admin-businesses') loadAdminData();
  if (name==='admin-audit') { loadAdminAudit(); loadCampaignUsage(); }
  if (name==='admin-controls') loadPlatformControls();
  if (name==='admin-cohorts') loadCohortsChurn();
  if (name==='orders') loadOrders();
  if (name==='products') loadProducts();
  if (name==='conversations') loadConversations();
  if (name==='overview') { loadCustomerStats(); loadRepeatCustomerStat(); try { loadSatisfactionScore(); } catch(_){} try { showShareStoreBanner(); } catch(_){} try { loadNeedsAttention(); } catch(_){} }
  if (name==='handoff') loadHandoffStats();
  if (name==='broadcast') { loadCustomers(); loadCampaignAudiences(); loadBcCustomerPicker(); updateBcRecipientPreview('all'); }
  if (name==='settings') { loadSettings(); loadTemplates(); }
  if (name==='crm') loadCrm();
  if (name==='reminders') loadReminders();
  if (name==='bookings') loadBookings();
}

// ════════════════════════════════════════════════════════════════════════════
// NEEDS ATTENTION (UI/UX enhancement) — dashboard overview panel answering
// "what needs my attention today?". Reuses already-existing endpoints
// (orders, products, handoff stats, conversations) rather than adding a new
// aggregating backend route. Each signal is fetched independently so one
// failing call never blanks the whole panel — it's just left out that time.
// Per audit rule #62 (no fake data): a signal that fails to load is simply
// omitted, never shown as a fabricated "0" or "—".
// ════════════════════════════════════════════════════════════════════════════
async function loadNeedsAttention() {
  const panel = document.getElementById('needs-attention-panel');
  const body  = document.getElementById('needs-attention-body');
  if (!panel || !body) return;

  const items = [];

  try {
    const raw = await apiFetch(ROUTES.orders);
    const orders = Array.isArray(raw) ? raw : (raw && raw.data ? raw.data : []);
    const awaitingPay = orders.filter(o => ['awaiting_payment','payment_review','pending_cash'].includes(o.status)).length;
    if (awaitingPay > 0) items.push({ icon: '💳', text: `${awaitingPay} order${awaitingPay!==1?'s':''} awaiting payment`, section: 'orders' });
    const toFulfill = orders.filter(o => ['confirmed','preparing','ready'].includes(o.status)).length;
    if (toFulfill > 0) items.push({ icon: '📦', text: `${toFulfill} order${toFulfill!==1?'s':''} need fulfillment`, section: 'orders' });
  } catch (_) { /* omit this signal rather than show a fake number */ }

  try {
    const raw = await apiFetch(ROUTES.products);
    const products = Array.isArray(raw) ? raw : (raw && raw.data ? raw.data : []);
    const lowStock = products.filter(p => _isProdOos(p) || _isProdLowStock(p)).length;
    if (lowStock > 0) items.push({ icon: '⚠️', text: `${lowStock} product${lowStock!==1?'s':''} low or out of stock`, section: 'products' });
  } catch (_) {}

  try {
    const hf = await apiFetch('/analytics/handoff-stats');
    if (hf && hf.active_count > 0) items.push({ icon: '🔴', text: `${hf.active_count} customer${hf.active_count!==1?'s':''} waiting for human help`, section: 'handoff' });
  } catch (_) {}

  try {
    const raw = await apiFetch(ROUTES.conversations);
    const convos = Array.isArray(raw) ? raw : (raw && raw.data ? raw.data : []);
    const unread = convos.reduce((s,c) => s + (c.unread_count||0), 0);
    if (unread > 0) items.push({ icon: '💬', text: `${unread} unread conversation${unread!==1?'s':''}`, section: 'conversations' });
  } catch (_) {}

  if (!items.length) { panel.style.display = 'none'; return; }
  panel.style.display = '';
  body.innerHTML = items.map(i => `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:12px;">
      <span>${i.icon} ${escHtml(i.text)}</span>
      <button class="btn btn-ghost" style="font-size:11px;padding:4px 10px;" onclick="showSection('${i.section}', null)">Review →</button>
    </div>`).join('');
}

// Accessibility: ESC closes the customer/order drawers, wherever the focus is.
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const cd = document.getElementById('customer-drawer');
  const od = document.getElementById('order-drawer');
  if (cd && cd.classList.contains('open')) closeDrawer();
  if (od && od.classList.contains('open')) closeOrderDrawer();
});

// ── UI/UX audit (Phase 11): drawer focus management ──────────────────────
// Opening a drawer only toggled a CSS class — focus stayed wherever it was
// (often back on the page behind an overlay), and closing never returned it
// to whatever triggered the drawer. A keyboard/screen-reader user opening a
// drawer got no indication focus had moved anywhere at all.
let _lastFocusedBeforeDrawer = null;

function _focusIntoDrawer(drawerId) {
  _lastFocusedBeforeDrawer = document.activeElement;
  const drawer = document.getElementById(drawerId);
  if (!drawer) return;
  const closeBtn = drawer.querySelector('.customer-drawer-close, [onclick*="close"]');
  if (closeBtn) closeBtn.focus();
}

function _restoreFocusFromDrawer() {
  if (_lastFocusedBeforeDrawer && typeof _lastFocusedBeforeDrawer.focus === 'function') {
    _lastFocusedBeforeDrawer.focus();
  }
  _lastFocusedBeforeDrawer = null;
}

// ── TOAST ─────────────────────────────────────────────────
function toast(msg, isError=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.borderLeftColor = isError ? 'var(--red)' : 'var(--green)';
  t.style.color = isError ? 'var(--red)' : 'var(--green)';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3500);
}

// ── UI/UX audit (Phase 11): shared debounce helper ───────────────────────
// filterOrdersBySearch/filterCrmTable/filterConversations each re-render
// their full list on every keystroke (a full array filter + innerHTML
// rebuild). Harmless at today's data sizes but wasteful, and would visibly
// lag on a business with a large order/customer history. This wraps a
// function so it only actually runs after typing pauses, without changing
// any of the three functions' own filtering/rendering logic.
function _debounce(fn, wait) {
  let t = null;
  return function(...args) {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), wait);
  };
}

// ── UI/UX audit (Phase 2): shared error+retry block ─────────────────────
// Every list load's catch block used to dead-end on a raw "⚠ <message>"
// with no way to recover except reloading the whole page. This produces
// the same friendly "Couldn't load X" + working [Retry] button everywhere,
// per the audit spec's Error States section, without changing any of the
// load functions' actual fetch/render logic.
function _errorBlock(what, retryFnName) {
  return `<div class="empty" style="color:var(--red);">
    ⚠ Couldn't load ${escHtml(what)}. Check your connection and try again.
    <div style="margin-top:8px;"><button class="btn btn-ghost" style="font-size:11px;padding:5px 12px;" onclick="${retryFnName}()">↻ Retry</button></div>
  </div>`;
}
function _errorRow(colspan, what, retryFnName) {
  return `<tr><td colspan="${colspan}">${_errorBlock(what, retryFnName)}</td></tr>`;
}

// Alias used by sprint-added functions
const showToast = toast;

// ── LIGHT / DARK MODE ────────────────────────────────────────────────────────
function toggleLightMode() {
  const html    = document.documentElement;
  const isLight = html.getAttribute('data-theme') === 'light';
  if (isLight) {
    html.removeAttribute('data-theme');
    try { localStorage.setItem('wazi_theme', 'dark'); } catch (e) {}
  } else {
    html.setAttribute('data-theme', 'light');
    try { localStorage.setItem('wazi_theme', 'light'); } catch (e) {}
  }
  _syncThemeToggleUI();
}

function _syncThemeToggleUI() {
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  const icon  = document.getElementById('theme-toggle-icon');
  const label = document.getElementById('theme-toggle-label');
  if (icon)  icon.textContent  = isLight ? '☀️' : '🌙';
  if (label) label.textContent = isLight ? 'Light Mode' : 'Dark Mode';
}
document.addEventListener('DOMContentLoaded', _syncThemeToggleUI);

// ── SUPERADMIN ────────────────────────────────────────────
let _adminBizList = [];   // cached for client-side search/filter on Manage Businesses

async function loadAdminData() {
  try {
    const [stats, businesses] = await Promise.all([
      apiFetch(ROUTES.adminStats),
      apiFetch(ROUTES.adminBiz)
    ]);
    if (!stats || !businesses) return;
    const _s = (id, val) => { const el=document.getElementById(id); if(el) el.textContent=val; };
    _s('sa-businesses', stats.businesses ?? '—');
    _s('sa-active', stats.active_businesses ?? '—');
    _s('sa-orders', stats.total_orders ?? '—');
    _s('sa-revenue', getCurrencySymbol() + (stats.total_revenue||0).toFixed(2));

    _s('sa-trialing', stats.trialing_count ?? '—');
    _s('sa-paid', stats.paid_count ?? '—');
    _s('sa-expired', stats.expired_trial_count ?? '—');
    const waSplit = document.getElementById('sa-wa-split');
    if (waSplit) waSplit.textContent = `${stats.dedicated_number_count ?? 0} / ${stats.shared_number_count ?? 0}`;

    const expEl = document.getElementById('sa-expiring-list');
    if (expEl) {
      const list = stats.expiring_soon || [];
      expEl.innerHTML = list.length
        ? list.map(b => `
            <div style="display:flex;justify-content:space-between;align-items:center;
                        padding:8px 10px;background:var(--surface2);border-radius:8px;
                        border-left:3px solid ${b.days_left<=2?'var(--red)':'var(--amber)'};">
              <span style="font-family:var(--mono);font-size:12px;font-weight:700;">${escHtml(b.name||'—')}</span>
              <span style="font-family:var(--mono);font-size:11px;color:${b.days_left<=2?'var(--red)':'var(--amber)'};">
                ${b.days_left===0?'Today':b.days_left+'d left'}
              </span>
            </div>`).join('')
        : `<div class="empty" style="padding:12px 0;">✅ No trials ending soon</div>`;
    }

    const catEl = document.getElementById('sa-category-breakdown');
    if (catEl) {
      const cats  = stats.by_category || {};
      const total = Object.values(cats).reduce((s,v) => s+v, 0) || 1;
      catEl.innerHTML = Object.entries(cats).map(([name, count]) => `
        <div style="display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:11px;">
          <span style="width:120px;color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escHtml(name)}</span>
          <div style="flex:1;background:var(--surface2);border-radius:3px;height:8px;overflow:hidden;">
            <div style="width:${Math.round(count/total*100)}%;background:var(--purple);height:100%;"></div>
          </div>
          <span style="width:24px;text-align:right;color:var(--text-dim);">${count}</span>
        </div>`).join('') || `<div class="empty" style="padding:8px 0;">No data yet</div>`;
    }

    const curEl = document.getElementById('sa-currency-breakdown');
    if (curEl) {
      const curs  = stats.by_currency || {};
      const total = Object.values(curs).reduce((s,v) => s+v, 0) || 1;
      curEl.innerHTML = Object.entries(curs).map(([name, count]) => `
        <div style="display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:11px;">
          <span style="width:60px;color:var(--text-dim);">${escHtml(name)}</span>
          <div style="flex:1;background:var(--surface2);border-radius:3px;height:8px;overflow:hidden;">
            <div style="width:${Math.round(count/total*100)}%;background:var(--blue);height:100%;"></div>
          </div>
          <span style="width:24px;text-align:right;color:var(--text-dim);">${count}</span>
        </div>`).join('') || `<div class="empty" style="padding:8px 0;">No data yet</div>`;
    }

    _adminBizList = Array.isArray(businesses) ? businesses : [];
    renderBizTable(_adminBizList, 'sa-biz-overview', false);
    applyAdminBizFilters();
  } catch(e) { toast('Failed to load admin data: ' + e.message, true); }

  loadAdminUsage();
}

// SuperAdmin 2.0 Phase 4 — message volume + AI usage/cost, loaded
// separately from the core stats so a slow/missing usage table never
// blocks the rest of the Overview from rendering.
async function loadAdminUsage() {
  const msgSummary = document.getElementById('sa-msg-summary');
  const msgTop      = document.getElementById('sa-msg-top');
  const aiSummary   = document.getElementById('sa-ai-summary');
  const aiTop       = document.getElementById('sa-ai-top');
  const aiNearLimit = document.getElementById('sa-ai-near-limit');

  try {
    const msg = await apiFetch(`${ROUTES.usageMessages}?days=7`);
    if (msgSummary) msgSummary.innerHTML =
      `<strong style="color:var(--text);">${msg.total_messages ?? 0}</strong> messages —
       ${msg.incoming ?? 0} in / ${msg.outgoing ?? 0} out${msg.sample_capped ? ' (capped sample)' : ''}`;
    if (msgTop) {
      const top = msg.top_businesses || [];
      msgTop.innerHTML = top.length
        ? top.map(b => `<div style="display:flex;justify-content:space-between;font-family:var(--mono);font-size:11px;">
            <span>${escHtml(b.name||('#'+b.business_id))}</span><span style="color:var(--text-dim);">${b.messages}</span>
          </div>`).join('')
        : `<div class="empty" style="padding:4px 0;">No message activity in this window.</div>`;
    }
  } catch (e) {
    if (msgSummary) msgSummary.textContent = 'Unavailable — ' + e.message;
  }

  try {
    const [ai, limits] = await Promise.all([
      apiFetch(`${ROUTES.usageAI}?hours=24`),
      apiFetch(ROUTES.usageLimits),
    ]);
    if (aiSummary) aiSummary.innerHTML = ai.tracking_available
      ? `<strong style="color:var(--text);">${ai.total_requests ?? 0}</strong> AI requests —
         ${(ai.total_tokens||0).toLocaleString()} tokens — $${(ai.total_estimated_cost||0).toFixed(4)} est. cost`
      : `<span style="color:var(--amber);">⚠️ ${escHtml(ai.note || 'AI usage tracking not available yet')}</span>`;
    if (aiTop) {
      const top = ai.top_businesses || [];
      aiTop.innerHTML = top.length
        ? top.map(b => `<div style="display:flex;justify-content:space-between;font-family:var(--mono);font-size:11px;">
            <span>${escHtml(b.name||('#'+b.business_id))}</span>
            <span style="color:var(--text-dim);">${b.requests} req · $${(b.estimated_cost||0).toFixed(4)}</span>
          </div>`).join('')
        : `<div class="empty" style="padding:4px 0;">No AI usage in this window.</div>`;
    }
    if (aiNearLimit) {
      const near = (limits && limits.businesses_near_ai_limit) || [];
      aiNearLimit.innerHTML = near.length
        ? `<div style="font-family:var(--mono);font-size:11px;color:var(--amber);margin-bottom:4px;">⚠️ Near daily AI limit:</div>` +
          near.map(b => `<div style="font-family:var(--mono);font-size:11px;display:flex;justify-content:space-between;">
            <span>${escHtml(b.name||('#'+b.business_id))}</span><span>${b.ai_requests_today}/${b.ai_daily_limit}</span>
          </div>`).join('')
        : '';
    }
  } catch (e) {
    if (aiSummary) aiSummary.textContent = 'Unavailable — ' + e.message;
  }
}

function applyAdminBizFilters() {
  const q       = (document.getElementById('sa-search')?.value || '').toLowerCase().trim();
  const status  = document.getElementById('sa-filter-status')?.value || '';
  const billing = document.getElementById('sa-filter-billing')?.value || '';

  let filtered = _adminBizList.filter(b => {
    if (q && !((b.name||'').toLowerCase().includes(q) || (b.owner_username||'').toLowerCase().includes(q))) return false;
    if (status === 'active' && !b.is_active) return false;
    if (status === 'suspended' && b.is_active) return false;
    if (billing) {
      const tier = (b.subscription_tier || 'free').toLowerCase();
      const bs   = (b.billing_status || '').toLowerCase();
      const isPaid = tier && tier !== 'free';
      if (billing === 'paid' && !isPaid) return false;
      if (billing === 'trialing' && (isPaid || bs !== 'trialing')) return false;
      if (billing === 'expired') {
        if (isPaid) return false;
        const end = b.trial_ends_at ? new Date(b.trial_ends_at) : null;
        if (!end || end > new Date()) return false;
      }
    }
    return true;
  });

  renderBizTable(filtered, 'sa-biz-table', true);
}

function renderBizTable(businesses, bodyId, showActions) {
  const tbody = document.getElementById(bodyId);
  if (!tbody) return;
  const biz = Array.isArray(businesses) ? businesses : [];
  if (!biz.length) { tbody.innerHTML=`<tr><td colspan="6"><div class="empty">No businesses match your filters.</div></td></tr>`; return; }
  tbody.innerHTML = biz.map(b => `<tr style="cursor:pointer;" onclick="openAdminBizDetail(${b.id})" title="Click for details">
    <td><span class="badge badge-purple">#${b.id}</span></td>
    <td><strong>${escHtml(b.name||'—')}</strong></td>
    <td><span class="badge badge-amber">${escHtml(b.owner_username||'—')}</span></td>
    <td style="color:var(--text-dim);font-size:11px">${escHtml(b.whatsapp_phone_id||'—')}</td>
    <td><span class="badge ${b.is_active?'badge-green':'badge-red'}">${b.is_active?'Active':'Suspended'}</span></td>
    <td onclick="event.stopPropagation()">${showActions?`<div style="display:flex;gap:6px;flex-wrap:wrap;">
      <button class="btn btn-ghost" style="color:var(--amber);border-color:rgba(245,158,11,0.3);" onclick="toggleBiz(${b.id},${b.is_active})">${b.is_active?'Suspend':'Activate'}</button>
      <button class="btn btn-ghost" onclick="deleteBiz(${b.id})">✕ Delete</button>
    </div>`:fmtTime(b.created_at || b.createdAt || b.timestamp)}</td>
  </tr>`).join('');
}

function openAdminBizDetail(id) {
  const b = _adminBizList.find(x => x.id === id);
  if (!b) { toast('Business details unavailable — refresh and try again', true); return; }

  const title = document.getElementById('sa-detail-title');
  const body  = document.getElementById('sa-detail-body');
  if (title) title.textContent = `${b.name || 'Business'} (#${b.id})`;

  const trialEnd = b.trial_ends_at ? new Date(b.trial_ends_at) : null;
  const daysLeft = trialEnd ? Math.ceil((trialEnd - new Date()) / 86400000) : null;
  const tier     = (b.subscription_tier || 'free');
  const isPaid   = tier && tier.toLowerCase() !== 'free';

  const row = (label, value) => `
    <div style="display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid var(--border);padding:4px 0;">
      <span style="color:var(--text-dim);">${label}</span>
      <span style="text-align:right;">${value}</span>
    </div>`;

  if (body) {
    body.innerHTML =
      row('Username', '@' + escHtml(b.owner_username || '—')) +
      row('Email', escHtml(b.owner_email || '—')) +
      row('Category', escHtml(b.category || '—')) +
      row('Currency', escHtml(b.currency || 'USD') + ' (' + escHtml(b.currency_symbol || '$') + ')') +
      row('WhatsApp', b.whatsapp_phone_id ? 'Dedicated — ' + escHtml(b.whatsapp_phone_id) : 'Shared number') +
      row('Business Type', b.is_service_business ? '🔧 Services' : '🛍️ Products') +
      row('Plan', isPaid ? `💳 ${escHtml(tier)}` : '🎁 Free Trial') +
      row('Billing Status', escHtml(b.billing_status || '—')) +
      (trialEnd ? row('Trial Ends', trialEnd.toLocaleDateString() + (daysLeft!=null ? ` (${daysLeft>=0?daysLeft+'d left':'expired '+(-daysLeft)+'d ago'})` : '')) : '') +
      row('Onboarding', b.onboarding_completed ? '✅ Completed' : '⏳ In progress') +
      row('Referral Code', escHtml(b.referral_code || '—')) +
      row('Created', fmtTime(b.created_at || b.createdAt));
  }

  const modal = document.getElementById('sa-biz-detail-modal');
  if (modal) modal.classList.add('open');

  // SuperAdmin 2.0 — Business 360: fetch the richer tenant-detail endpoint
  // (usage, admin notes, risk flags, recent audit log for this business)
  // without blocking the modal from opening with what we already have.
  _saCurrentDetailId = id;
  loadAdminBizDetailExtra(id);
}

let _saCurrentDetailId = null;

async function loadAdminBizDetailExtra(id) {
  const usageEl = document.getElementById('sa-detail-usage');
  const flagsEl = document.getElementById('sa-detail-flags');
  const notesEl = document.getElementById('sa-detail-notes');
  const auditEl = document.getElementById('sa-detail-audit');
  [usageEl, flagsEl, notesEl, auditEl].forEach(el => { if (el) el.textContent = ''; });
  try {
    const detail = await apiFetch(`${ROUTES.saasTenant}/${id}`);
    if (!detail || _saCurrentDetailId !== id) return; // stale response, modal moved on

    const u = detail.usage || {};
    if (usageEl) usageEl.innerHTML =
      `<div style="display:flex;gap:14px;flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--text-dim);">
        <span>📦 ${u.products ?? '—'} products</span>
        <span>🛒 ${u.orders ?? '—'} orders</span>
        <span>👥 ${u.customers ?? '—'} customers</span>
        <span>💬 ${u.messages ?? '—'} messages</span>
        <span>🗓️ ${u.bookings ?? '—'} bookings</span>
      </div>`;

    const flags = detail.risk_flags || [];
    if (flagsEl) flagsEl.innerHTML = flags.length
      ? flags.map(f => `<div style="display:flex;justify-content:space-between;gap:8px;padding:4px 0;border-bottom:1px solid var(--border);">
          <span><span class="badge ${f.risk_level==='high'?'badge-red':f.risk_level==='medium'?'badge-amber':'badge-blue'}">${escHtml(f.risk_level)}</span> ${escHtml(f.reason||'')} <span style="color:var(--text-dim);">(${escHtml(f.status||'open')})</span></span>
          ${f.status==='open' ? `<button class="btn btn-ghost" style="padding:2px 8px;font-size:10px;" onclick="resolveRiskFlag(${f.id})">Resolve</button>` : ''}
        </div>`).join('')
      : `<div class="empty" style="padding:4px 0;">No risk flags on this business.</div>`;

    const notes = detail.admin_notes || [];
    if (notesEl) notesEl.innerHTML = notes.length
      ? notes.map(n => `<div style="padding:4px 0;border-bottom:1px solid var(--border);">
          <span style="color:var(--text-dim);">${fmtTime(n.created_at)} — @${escHtml(n.author_username||'—')}:</span> ${escHtml(n.note)}
        </div>`).join('')
      : `<div class="empty" style="padding:4px 0;">No notes yet.</div>`;

    const logs = detail.recent_audit_logs || [];
    if (auditEl) auditEl.innerHTML = logs.length
      ? logs.map(l => `<div style="padding:3px 0;color:var(--text-dim);">${fmtTime(l.created_at)} — @${escHtml(l.actor_username||'—')} <strong style="color:var(--text);">${escHtml(l.action||'')}</strong>${l.reason?' — '+escHtml(l.reason):''}</div>`).join('')
      : `<div class="empty" style="padding:4px 0;">No admin actions recorded yet.</div>`;
  } catch (e) {
    if (usageEl) usageEl.innerHTML = `<div class="empty">Usage/audit data unavailable — ${escHtml(e.message||'')}</div>`;
    if (flagsEl) flagsEl.textContent = '—';
    if (notesEl) notesEl.textContent = '—';
    if (auditEl) auditEl.textContent = '—';
  }
}

async function addAdminNote() {
  const input = document.getElementById('sa-note-input');
  const note  = (input?.value || '').trim();
  if (!note) { toast('Enter a note first', true); return; }
  if (!_saCurrentDetailId) return;
  try {
    await apiFetch(`${ROUTES.saasTenant}/${_saCurrentDetailId}/notes?note=${encodeURIComponent(note)}`, { method: 'POST' });
    if (input) input.value = '';
    toast('Note added');
    loadAdminBizDetailExtra(_saCurrentDetailId);
  } catch (e) { toast('Failed to add note: ' + e.message, true); }
}

async function resolveRiskFlag(flagId) {
  try {
    await apiFetch(`${ROUTES.riskFlags}/${flagId}/resolve?status=cleared`, { method: 'POST' });
    toast('Flag cleared');
    if (_saCurrentDetailId) loadAdminBizDetailExtra(_saCurrentDetailId);
    loadAdminAudit();
  } catch (e) { toast('Failed to resolve flag: ' + e.message, true); }
}

function closeAdminBizDetail() {
  const modal = document.getElementById('sa-biz-detail-modal');
  if (modal) modal.classList.remove('open');
  _saCurrentDetailId = null;
}

// ── SuperAdmin 2.0 — Audit & Abuse section ──────────────────────────────────

async function loadAdminAudit() {
  const flagsTbody = document.getElementById('sa-risk-flags-table');
  const auditTbody = document.getElementById('sa-audit-log-table');
  try {
    const [flagsRes, logsRes] = await Promise.all([
      apiFetch(`${ROUTES.riskFlags}?status=open`),
      apiFetch(`${ROUTES.auditLogs}?limit=50`),
    ]);
    const flags = (flagsRes && flagsRes.flags) || [];
    const logs  = (logsRes && logsRes.logs) || [];

    if (flagsTbody) {
      flagsTbody.innerHTML = flags.length ? flags.map(f => `<tr>
          <td><span class="badge badge-purple">#${f.business_id}</span></td>
          <td><span class="badge ${f.risk_level==='high'?'badge-red':f.risk_level==='medium'?'badge-amber':'badge-blue'}">${escHtml(f.risk_level)}</span></td>
          <td>${escHtml(f.reason||'')}</td>
          <td style="color:var(--text-dim);font-size:11px;">${escHtml(JSON.stringify(f.evidence||{}))}</td>
          <td>${fmtTime(f.created_at)}</td>
          <td><button class="btn btn-ghost" onclick="resolveRiskFlag(${f.id})">Resolve</button></td>
        </tr>`).join('') : `<tr><td colspan="6"><div class="empty">No open risk flags. Run a scan to check for new ones.</div></td></tr>`;
    }
    if (auditTbody) {
      auditTbody.innerHTML = logs.length ? logs.map(l => `<tr>
          <td style="color:var(--text-dim);font-size:11px;">${fmtTime(l.created_at)}</td>
          <td>@${escHtml(l.actor_username||'—')}</td>
          <td><span class="badge badge-purple">${escHtml(l.action||'')}</span></td>
          <td>${l.business_id!=null?'#'+l.business_id:'—'}</td>
          <td style="color:var(--text-dim);">${escHtml(l.reason||'—')}</td>
        </tr>`).join('') : `<tr><td colspan="5"><div class="empty">No admin actions recorded yet.</div></td></tr>`;
    }
  } catch (e) {
    toast('Failed to load audit/abuse data: ' + e.message, true);
  }

  loadAdminSecurity();
}

// SuperAdmin 2.0 Phase 6 — live security counters snapshot. Explicitly
// labeled as a point-in-time view (the backend resets these on every
// redeploy — no persisted history exists yet).
async function loadAdminSecurity() {
  const summaryEl = document.getElementById('sa-security-summary');
  const detailsEl = document.getElementById('sa-security-details');
  try {
    const s = await apiFetch(ROUTES.security);
    if (summaryEl) summaryEl.innerHTML = `
      <span>🔒 <strong>${(s.locked_accounts||[]).length}</strong> locked accounts</span>
      <span>🚫 <strong>${(s.flagged_login_ips||[]).length}</strong> flagged IPs</span>
      <span>📝 <strong>${s.active_failed_login_pairs ?? 0}</strong> recent failed-login pairs</span>
      <span>🆕 <strong>${(s.suspicious_signup_ips||[]).length}</strong> suspicious signup IPs</span>
      <span>🪝 <strong>${s.webhook_invalid_signatures_24h ?? 0}</strong> invalid webhook signatures (24h)</span>
      <span>⚠️ <strong>${s.open_risk_flags_count ?? 0}</strong> open risk flags</span>`;

    const rows = [];
    (s.locked_accounts||[]).forEach(a => rows.push(`🔒 Account <strong>@${escHtml(a.username)}</strong> locked — ${a.failed_attempts} failed attempts`));
    (s.flagged_login_ips||[]).forEach(f => rows.push(`🚫 IP <strong>${escHtml(f.ip)}</strong> flagged — ${f.failed_attempts} failed login attempts`));
    (s.suspicious_signup_ips||[]).forEach(sg => rows.push(`🆕 IP <strong>${escHtml(sg.ip)}</strong> — ${sg.attempts} signup attempts (${escHtml(sg.limit)})`));
    if (detailsEl) detailsEl.innerHTML = rows.length ? rows.map(r => `<div>${r}</div>`).join('') : `<div class="empty">No active security concerns right now.</div>`;
  } catch (e) {
    if (summaryEl) summaryEl.textContent = 'Security data unavailable — ' + e.message;
  }
}

async function runAbuseScan() {
  try {
    const res = await apiFetch(ROUTES.abuseScan, { method: 'POST' });
    toast(`Scan complete — ${res.new_flags} new flag(s) from ${res.scanned_businesses} businesses`);
    loadAdminAudit();
  } catch (e) { toast('Abuse scan failed: ' + e.message, true); }
}

// ── SuperAdmin 2.0 Phase 8 — Emergency Platform Controls & Feature Flags ────

async function loadPlatformControls() {
  const pauseTbody = document.getElementById('sa-pause-flags-table');
  const maintEl    = document.getElementById('sa-maintenance-status');
  const flagsTbody = document.getElementById('sa-feature-flags-table');

  try {
    const controls = await apiFetch(ROUTES.platformControls);
    if (pauseTbody) {
      const flags = controls.pause_flags || [];
      pauseTbody.innerHTML = flags.map(f => `<tr>
          <td>${escHtml(f.label)}</td>
          <td><span class="badge ${f.paused?'badge-red':'badge-green'}">${f.paused?'PAUSED':'Running'}</span></td>
          <td><button class="btn btn-ghost" style="${f.paused?'':'color:var(--red);border-color:rgba(239,68,68,0.3);'}"
                onclick="togglePauseFlag('${f.flag}', ${!f.paused}, '${escHtml(f.label)}')">${f.paused?'Resume':'Pause'}</button></td>
        </tr>`).join('');
    }
    if (maintEl) {
      const m = controls.maintenance_mode || {enabled:false};
      maintEl.innerHTML = `Status: <span class="badge ${m.enabled?'badge-red':'badge-green'}">${m.enabled?'ENABLED':'Disabled'}</span>` +
        (m.enabled && m.message ? ` — "${escHtml(m.message)}"` : '');
    }
  } catch (e) {
    if (pauseTbody) pauseTbody.innerHTML = `<tr><td colspan="3"><div class="empty">Unavailable — ${escHtml(e.message)}</div></td></tr>`;
  }

  try {
    const res = await apiFetch(ROUTES.featureFlags);
    const flags = res.flags || [];
    if (flagsTbody) {
      flagsTbody.innerHTML = flags.length ? flags.map(f => `<tr>
          <td>${escHtml(f.name)}</td>
          <td><span class="badge ${f.enabled?'badge-green':'badge-red'}">${f.enabled?'Enabled':'Disabled'}</span></td>
          <td style="color:var(--text-dim);font-size:11px;">${f.updated_by?('@'+escHtml(f.updated_by)):'—'}</td>
          <td><button class="btn btn-ghost" onclick="setFeatureFlag('${escHtml(f.name)}', ${!f.enabled})">${f.enabled?'Disable':'Enable'}</button></td>
        </tr>`).join('') : `<tr><td colspan="4"><div class="empty">No feature flags set yet.</div></td></tr>`;
    }
  } catch (e) {
    if (flagsTbody) flagsTbody.innerHTML = `<tr><td colspan="4"><div class="empty">Unavailable — ${escHtml(e.message)}</div></td></tr>`;
  }
}

async function togglePauseFlag(flag, paused, label) {
  let reason = '';
  if (paused) {
    if (!confirm(`⚠️ This will PAUSE "${label}" platform-wide, for every business, right now.\n\nAre you sure?`)) return;
    reason = prompt(`Reason for pausing "${label}" (required):`, '');
    if (!reason || !reason.trim()) { toast('A reason is required to pause a platform control', true); return; }
  } else {
    if (!confirm(`Resume "${label}" platform-wide?`)) return;
  }
  try {
    const qs = new URLSearchParams({ paused: String(paused), reason: reason || '' });
    await apiFetch(`${ROUTES.platformControls}/${flag}?${qs.toString()}`, { method: 'POST' });
    toast(paused ? `"${label}" paused` : `"${label}" resumed`);
    loadPlatformControls();
  } catch (e) { toast('Failed to update control: ' + e.message, true); }
}

async function toggleMaintenanceMode(enabled) {
  const message = document.getElementById('sa-maintenance-message')?.value || '';
  let reason = '';
  if (enabled) {
    if (!confirm('⚠️ Enable platform-wide maintenance mode?')) return;
    reason = prompt('Reason for enabling maintenance mode (required):', '');
    if (!reason || !reason.trim()) { toast('A reason is required', true); return; }
  }
  try {
    const qs = new URLSearchParams({ enabled: String(enabled), message, reason: reason || '' });
    await apiFetch(`${ROUTES.platformMaintenance}?${qs.toString()}`, { method: 'POST' });
    toast(enabled ? 'Maintenance mode enabled' : 'Maintenance mode disabled');
    loadPlatformControls();
  } catch (e) { toast('Failed to update maintenance mode: ' + e.message, true); }
}

async function setFeatureFlag(name, enabled) {
  name = (name || '').trim();
  if (!name) { toast('Enter a flag name first', true); return; }
  try {
    await apiFetch(`${ROUTES.featureFlags}/${encodeURIComponent(name)}?enabled=${enabled}`, { method: 'POST' });
    toast(`Flag "${name}" ${enabled ? 'enabled' : 'disabled'}`);
    const input = document.getElementById('sa-new-flag-name');
    if (input) input.value = '';
    loadPlatformControls();
  } catch (e) { toast('Failed to update flag: ' + e.message, true); }
}

// ── SuperAdmin 2.0 Phase 10 — Campaign Volume / Abuse Signal ────────────────

async function loadCampaignUsage() {
  const summaryEl = document.getElementById('sa-campaign-usage-summary');
  const tbody     = document.getElementById('sa-campaign-usage-table');
  try {
    const res = await apiFetch(`${ROUTES.usageCampaigns}?days=7`);
    if (summaryEl) {
      summaryEl.innerHTML = `<strong>${res.total_sends}</strong> campaign send(s) · <strong>${res.total_recipients}</strong> total recipients in the last ${res.window_days} days`;
    }
    if (tbody) {
      const rows = res.top_businesses || [];
      tbody.innerHTML = rows.length ? rows.map(b => `<tr>
          <td>${escHtml(b.name || ('#'+b.business_id))}</td>
          <td>${b.sends}</td>
          <td>${b.recipients}</td>
        </tr>`).join('') : `<tr><td colspan="3"><div class="empty">No campaigns sent in this window.</div></td></tr>`;
    }
  } catch (e) {
    if (summaryEl) summaryEl.innerHTML = `<div class="empty">Unavailable — ${escHtml(e.message)}</div>`;
    if (tbody) tbody.innerHTML = '';
  }
}

// ── SuperAdmin 2.0 Phase 9 — Cohorts & Churn ────────────────────────────────

async function loadCohortsChurn() {
  const cohortsTbody = document.getElementById('sa-cohorts-table');
  const byTierEl      = document.getElementById('sa-churn-by-tier');
  const recentNoteEl  = document.getElementById('sa-churn-recent-note');

  try {
    const churn = await apiFetch(ROUTES.churn);
    const at = churn.all_time || {};
    document.getElementById('sa-churn-rate').textContent = `${at.churn_rate_pct ?? 0}%`;
    document.getElementById('sa-churn-cancelled').textContent = at.cancelled_count ?? 0;
    document.getElementById('sa-churn-cancelled-sub').textContent = `of ${at.ever_paid_count ?? 0} ever-paid`;
    document.getElementById('sa-churn-30d').textContent =
      (churn.recent && churn.recent.last_30_days != null) ? churn.recent.last_30_days : '—';

    if (byTierEl) {
      const entries = Object.entries(at.churn_by_tier || {});
      byTierEl.innerHTML = entries.length
        ? entries.map(([tier,count]) => `<span style="margin-right:18px;">${escHtml(tier)}: <strong>${count}</strong></span>`).join('')
        : '<span style="color:var(--text-dim);">No cancellations yet.</span>';
    }
    if (recentNoteEl) {
      recentNoteEl.textContent = churn.recent && churn.recent.tracking_since
        ? `Recent-churn windows track cancellations since ${new Date(churn.recent.tracking_since).toLocaleDateString()}.`
        : (churn.recent && churn.recent.note) || '';
    }
  } catch (e) {
    if (byTierEl) byTierEl.innerHTML = `<div class="empty">Unavailable — ${escHtml(e.message)}</div>`;
  }

  try {
    const res = await apiFetch(ROUTES.cohorts);
    const cohorts = res.cohorts || [];
    if (cohortsTbody) {
      cohortsTbody.innerHTML = cohorts.length ? cohorts.map(c => `<tr>
          <td>${escHtml(c.cohort_month)}</td>
          <td>${c.signups}</td>
          <td>${c.trialing}</td>
          <td style="color:var(--green);">${c.active}</td>
          <td style="color:var(--amber);">${c.past_due}</td>
          <td style="color:var(--red);">${c.cancelled}</td>
          <td>${c.still_here_pct}%</td>
        </tr>`).join('') : `<tr><td colspan="7"><div class="empty">No businesses yet.</div></td></tr>`;
    }
  } catch (e) {
    if (cohortsTbody) cohortsTbody.innerHTML = `<tr><td colspan="7"><div class="empty">Unavailable — ${escHtml(e.message)}</div></td></tr>`;
  }
}

function openModal() { document.getElementById('add-business-modal').classList.add('open'); }
function closeModal() { document.getElementById('add-business-modal').classList.remove('open'); }

async function createBusiness() {
  const name     = document.getElementById('b-name').value.trim();
  const username = document.getElementById('b-username').value.trim();
  const password = document.getElementById('b-password').value.trim();
  const category = document.getElementById('b-category')?.value || '';
  if (!name||!username||!password) { toast('Name, username and password required',true); return; }
  try {
    await apiFetch(ROUTES.register, {
      method: 'POST',
      body: JSON.stringify({
        business_name: name,
        username,
        password,
        category: category || undefined,
        use_shared_number: true,
      })
    });
    toast(`✅ ${name} created — uses shared WhatsApp number`);
    closeModal();
    ['b-name','b-username','b-password'].forEach(id=>{const el=document.getElementById(id);if(el)el.value='';});
    loadAdminData();
  } catch(e) { toast('Failed: '+e.message, true); }
}

async function toggleBiz(id, active) {
  try { await apiFetch(`${ROUTES.adminBiz}/${id}`, {method:'PATCH', body:JSON.stringify({is_active:!active})}); toast(active?'Suspended':'Activated'); loadAdminData(); }
  catch(e) { toast('Failed',true); }
}

async function deleteBiz(id) {
  if (!confirm('Delete this business and ALL their data?')) return;
  try { await apiFetch(`${ROUTES.adminBiz}/${id}`, {method:'DELETE'}); toast('Deleted'); loadAdminData(); }
  catch(e) { toast('Failed',true); }
}

// ── ORDERS ────────────────────────────────────────────────
async function loadOrders() {
  try {
    const raw = await apiFetch(ROUTES.orders);
    if (!raw) return;
    const orders = Array.isArray(raw) ? raw : (Array.isArray(raw.data) ? raw.data : []);
    renderOrders(orders, 'orders-body', true);
    renderOrders(orders.slice(0,5), 'recent-orders-body', false);
    const statO = document.getElementById('stat-orders');
    const statR = document.getElementById('stat-revenue');
    if (statO) statO.textContent = orders.length;
    if (statR) statR.textContent = getCurrencySymbol() + orders.reduce((s,o)=>s+(o.total_price||0),0).toFixed(2);
  } catch(e) {
    ['orders-body','recent-orders-body'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = _errorRow(7, 'orders', 'loadOrders');
    });
  }
}

// ── Order status labels — separate sets for product vs service businesses ──
// Previously the status filter dropdown always showed the full product-
// fulfillment lifecycle (Preparing/Ready/Out for Delivery/Delivered) even
// for a barber shop or salon, where those steps don't apply — a haircut
// isn't "prepared" or "delivered". The actual appointment/booking status
// (confirmed, completed, no-show, etc.) is already tracked separately in
// the Bookings section; this ORDER status is purely about payment/order
// lifecycle, so the service-business set is intentionally shorter.
const _ORDER_STATUS_LABELS = {
  all: 'All Statuses', pending: 'Pending', pending_cash: 'Confirmed (Cash)',
  awaiting_payment: 'Awaiting Payment', payment_review: 'Payment Review',
  confirmed: 'Confirmed', preparing: 'Preparing', ready: 'Ready',
  out_for_delivery: 'Out for Delivery', delivered: 'Delivered',
  completed: 'Completed', cancelled: 'Cancelled', refunded: 'Refunded',
};
const _ORDER_STATUS_LABELS_SERVICE = {
  all: 'All Statuses', pending: 'Pending', pending_cash: 'Confirmed (Cash)',
  awaiting_payment: 'Awaiting Payment', payment_review: 'Payment Review',
  confirmed: 'Confirmed', completed: 'Completed', cancelled: 'Cancelled', refunded: 'Refunded',
};
// Filter option ORDER per business type — product keeps every existing
// status; service drops the delivery-fulfillment-only steps.
// UI/UX audit: previously missing awaiting_payment/payment_review/refunded,
// which the backend (order_lifecycle.py VALID_STATUSES) already supports —
// orders in those states rendered with a raw/blank-looking status badge.
const _ORDER_STATUS_KEYS_PRODUCT = ['all','pending','pending_cash','awaiting_payment','payment_review','confirmed','preparing','ready','out_for_delivery','delivered','completed','cancelled','refunded'];
const _ORDER_STATUS_KEYS_SERVICE = ['all','pending','pending_cash','awaiting_payment','payment_review','confirmed','completed','cancelled','refunded'];

function _populateOrderStatusFilter() {
  const sel = document.getElementById('order-status-filter');
  if (!sel) return;
  const isService = !!window.IS_SERVICE_BUSINESS;
  const keys   = isService ? _ORDER_STATUS_KEYS_SERVICE : _ORDER_STATUS_KEYS_PRODUCT;
  const labels = isService ? _ORDER_STATUS_LABELS_SERVICE : _ORDER_STATUS_LABELS;
  const prevValue = sel.value || 'all';
  sel.innerHTML = keys.map(k => `<option value="${k}">${labels[k]}</option>`).join('');
  // Keep the previous selection if it's still valid for this business type,
  // otherwise fall back to "all" rather than silently landing on something
  // that no longer exists in the list.
  sel.value = keys.includes(prevValue) ? prevValue : 'all';
}

function renderOrders(orders, bodyId, showStatus) {
  // Bulk-select checkboxes only make sense on the full Orders list, not the
  // 5-row "Recent Orders" widget on the dashboard overview.
  const withCheckbox = bodyId === 'orders-body';
  const cols = (showStatus ? 7 : 6) + (withCheckbox ? 1 : 0);
  const tbody = document.getElementById(bodyId);
  if (!tbody) return;
  const rows = Array.isArray(orders) ? orders : [];
  if (!rows.length){tbody.innerHTML=`<tr><td colspan="${cols}"><div class="empty">No orders yet.<br><span style="font-size:11px;color:var(--text-dim);">Orders from your WhatsApp customers will appear here.</span></div></td></tr>`;return;}
  tbody.innerHTML=rows.map(o=>{
    const status = o.status || 'pending';
    const checkboxCell = withCheckbox
      ? `<td onclick="event.stopPropagation();"><input type="checkbox" class="order-checkbox" data-id="${o.id}" ${_selectedOrderIds.has(o.id)?'checked':''} onchange="toggleOrderSelect(${o.id})" aria-label="Select order #${o.id}"/></td>`
      : '';
    return `<tr onclick="openOrderDrawer(${o.id})" style="cursor:pointer;" title="Click to view order details">
    ${checkboxCell}
    <td><span class="badge badge-amber">#${o.id||'—'}</span></td>
    <td>${escHtml(o.customer_phone||'—')}</td>
    <td>${escHtml(o.product_name||'—')}</td>
    <td>${o.quantity||0}</td>
    <td><span class="badge badge-green">${getCurrencySymbol()}${(o.total_price||0).toFixed(2)}</span></td>
    ${showStatus?`<td><span class="badge ${status==='pending'?'badge-amber':'badge-green'}">${escHtml((window.IS_SERVICE_BUSINESS ? _ORDER_STATUS_LABELS_SERVICE[status] : _ORDER_STATUS_LABELS[status]) || status)}</span></td>`:''}
    <td>${fmtTime(o.created_at || o.createdAt || o.timestamp)}</td>
  </tr>`;
  }).join('');
}

// ── PRODUCTS ──────────────────────────────────────────────
let _productView = 'table';
let _pendingImgDataUrl = null;

function setProductView(mode) {
  _productView = mode;
  const tableEl = document.getElementById('products-table');
  const gridEl  = document.getElementById('products-grid');
  const tbBtn   = document.getElementById('view-table-btn');
  const gbBtn   = document.getElementById('view-grid-btn');
  if (tableEl) tableEl.style.display = mode === 'table' ? '' : 'none';
  if (gridEl)  gridEl.style.display  = mode === 'grid'  ? '' : 'none';
  if (tbBtn) tbBtn.style.opacity = mode === 'table' ? '1' : '0.5';
  if (gbBtn) gbBtn.style.opacity = mode === 'grid'  ? '1' : '0.5';
}

// Internal product cache for filtering
let _allProducts = [];
let _selectedProductIds = new Set();

async function loadProducts() {
  const skeleton = document.getElementById('prod-skeleton');
  const tbody    = document.getElementById('products-body');
  if (skeleton) skeleton.style.display = '';
  if (tbody)    tbody.innerHTML = '';

  try {
    const raw = await apiFetch(ROUTES.products);
    if (skeleton) skeleton.style.display = 'none';
    if (!raw) return;
    _allProducts = Array.isArray(raw) ? raw : (Array.isArray(raw.data) ? raw.data : []);

    // KPI update (Phase 1)
    _updateProductKPIs(_allProducts);

    // Populate the category filter from categories actually in use,
    // rather than a static list — keeps it relevant to this business's
    // real products and picks up custom/typed-in categories automatically.
    _populateCategoryFilter(_allProducts);

    // Apply any active filters then render
    applyProductFilters();

    // Load analytics after products (Phase 8)
    loadProductAnalytics();
  } catch(e) {
    if (skeleton) skeleton.style.display = 'none';
    if (tbody) tbody.innerHTML = _errorRow(8, 'products', 'loadProducts');
  }
}

function _updateProductKPIs(products) {
  const total   = products.length;
  const active  = products.filter(p => !_isProdOos(p) && p.status !== 'draft').length;
  const oos     = products.filter(p => _isProdOos(p)).length;
  _setKpi('kpi-total-products', total);
  _setKpi('kpi-active-products', active);
  _setKpi('kpi-oos-products', oos);
  _setKpi('stat-products', total);
  const statO = document.getElementById('kpi-prod-orders');
  const statR = document.getElementById('kpi-prod-revenue');
  const srcO  = document.getElementById('stat-orders');
  const srcR  = document.getElementById('stat-revenue');
  if (statO && srcO) statO.textContent = srcO.textContent;
  if (statR && srcR) statR.textContent = srcR.textContent;
  _updateImageCoverage(products);
}

// Phase 11: Image coverage KPI
function _updateImageCoverage(products) {
  const total      = products.length;
  const withImages = products.filter(p => p.image_url).length;
  const missing    = total - withImages;
  const pct        = total ? Math.round(withImages / total * 100) : 0;
  const imgKpi = document.getElementById('kpi-img-coverage');
  if (imgKpi) imgKpi.textContent = pct + '%';
  const missingBadge = document.getElementById('prod-missing-img-badge');
  if (missingBadge) missingBadge.textContent = missing > 0 ? missing : '';
  const nudgeEl = document.getElementById('img-coverage-nudge');
  if (nudgeEl) {
    nudgeEl.style.display = (missing > 0 && total > 0) ? '' : 'none';
    nudgeEl.textContent   = missing > 0
      ? '\uD83D\uDCF8 ' + missing + ' product' + (missing > 1 ? 's' : '') + ' missing images — products with images get more interactions.'
      : '';
  }
}
function _setKpi(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}
function _isProdOos(p) {
  // Service businesses don't track stock — never show "Out of Stock",
  // regardless of what a product's stored status/stock says (it may be
  // stale from before the business switched from Products to Services mode).
  if (window.IS_SERVICE_BUSINESS) return false;
  if (p.status === 'out_of_stock') return true;
  if (typeof p.stock === 'number' && p.stock === 0) return true;
  return false;
}
function _isProdLowStock(p) {
  if (window.IS_SERVICE_BUSINESS) return false;
  // UI/UX audit: the backend already stores a real, configurable
  // low_stock_threshold per product (default 5) and uses it server-side
  // (crud.get_low_stock_products) — this used to hardcode "<= 5" for every
  // product regardless of its own threshold, so a product a business had
  // deliberately set a higher/lower threshold for showed the wrong badge.
  const threshold = (typeof p.low_stock_threshold === 'number') ? p.low_stock_threshold : 5;
  return typeof p.stock === 'number' && p.stock > 0 && p.stock <= threshold;
}
function _prodStatusBadge(p) {
  if (_isProdOos(p))         return `<span class="prod-status-oos">● Out of Stock</span>`;
  if (_isProdLowStock(p))    return `<span class="prod-status-low">⚠ Low Stock</span>`;
  if (p.status === 'draft')  return `<span class="prod-status-draft">○ Draft</span>`;
  return `<span class="prod-status-active">● Active</span>`;
}

/**
 * Rebuild the "All Categories" filter dropdown from the categories actually
 * present on this business's products, instead of a static hardcoded list.
 * - Options are sorted alphabetically for easy scanning.
 * - The currently-selected filter value is preserved across reloads/re-adds
 *   (e.g. after adding a new product) so an active filter doesn't silently
 *   reset itself.
 * - If the previously-selected category no longer exists in the product
 *   list (e.g. the last product in that category was deleted), falls back
 *   to "All Categories" rather than leaving a stale/invalid selection.
 */
function _populateCategoryFilter(products) {
  const sel = document.getElementById('prod-filter-cat');
  if (!sel) return;

  const prevValue = sel.value;

  const categories = [...new Set(
    (products || [])
      .map(p => (p.category || '').trim())
      .filter(Boolean)
  )].sort((a, b) => a.localeCompare(b));

  sel.innerHTML = '<option value="">All Categories</option>' +
    categories.map(c => `<option value="${escHtml(c)}">${escHtml(c)}</option>`).join('');

  // Restore prior selection if it's still a valid option; otherwise reset to "All"
  const stillValid = !prevValue || categories.some(c => c.toLowerCase() === prevValue.toLowerCase());
  sel.value = stillValid ? prevValue : '';
}

function applyProductFilters() {
  const q      = (document.getElementById('prod-search')?.value || '').toLowerCase();
  const cat    = (document.getElementById('prod-filter-cat')?.value || '').toLowerCase();
  const status = document.getElementById('prod-filter-status')?.value || '';
  const sort   = document.getElementById('prod-sort')?.value || 'newest';

  let filtered = _allProducts.filter(p => {
    if (q && !((p.name||'').toLowerCase().includes(q) || (p.category||'').toLowerCase().includes(q))) return false;
    if (cat && (p.category||'').toLowerCase() !== cat) return false;
    if (status === 'active'       && (_isProdOos(p) || p.status === 'draft')) return false;
    if (status === 'out_of_stock' && !_isProdOos(p))          return false;
    if (status === 'low_stock'    && !_isProdLowStock(p))      return false;
    if (status === 'missing_image'&& p.image_url)              return false;
    if (status === 'has_image'    && !p.image_url)             return false;
    return true;
  });

  // Sort
  if (sort === 'oldest')      filtered = [...filtered].reverse();
  if (sort === 'price_asc')   filtered.sort((a,b) => (a.price||0)-(b.price||0));
  if (sort === 'price_desc')  filtered.sort((a,b) => (b.price||0)-(a.price||0));

  _renderProductTable(filtered);
  _renderProductGrid(filtered);
}

function _renderProductTable(products) {
  const tbody = document.getElementById('products-body');
  if (!tbody) return;
  if (!products.length) {
    tbody.innerHTML = `<tr><td colspan="8"><div class="empty" style="padding:24px;text-align:center;">
      <div style="font-size:32px;margin-bottom:8px;">📦</div>
      <div style="font-size:13px;font-weight:700;margin-bottom:4px;">No products found</div>
      <div style="font-size:11px;color:var(--text-dim);">Try adjusting your search or filters, or add a new product above.</div>
    </div></td></tr>`;
    return;
  }
  tbody.innerHTML = products.map(p => {
    const checked = _selectedProductIds.has(p.id) ? 'checked' : '';
    const thumb   = p.image_url
      ? `<img class="product-thumb" src="${escHtml(p.image_url)}" alt="${escHtml(p.name||'')}" onerror="this.style.display='none';this.nextSibling.style.display='flex'"><div class="product-thumb-placeholder" style="display:none">📦</div>`
      : `<div class="product-thumb-placeholder">📦</div>`;
    const stockDisplay = window.IS_SERVICE_BUSINESS
      ? `<span style="color:var(--text-dim);font-size:11px;" title="Stock tracking is off for service businesses">N/A</span>`
      : (typeof p.stock === 'number'
          ? `<span style="font-size:12px;font-family:var(--mono);${p.stock <= 5 ? 'color:var(--amber)' : ''}">${p.stock}</span>`
          : `<span style="color:var(--text-dim);font-size:11px;">—</span>`);
    const catDisplay = p.category
      ? `<span style="font-size:11px;color:var(--text-dim);font-family:var(--mono);">${escHtml(p.category)}</span>`
      : `<span style="color:var(--text-dim);font-size:11px;">—</span>`;
    return `<tr>
      <td><input type="checkbox" class="prod-cb" ${checked} onchange="toggleProductSelect(${p.id},this.checked)"/></td>
      <td style="width:44px">${thumb}</td>
      <td><strong style="font-size:13px;">${escHtml(p.name||'—')}</strong>${p.description?`<div style="font-size:10px;color:var(--text-dim);font-family:var(--mono);margin-top:2px;">${escHtml(p.description.substring(0,60))}${p.description.length>60?'…':''}</div>`:''}</td>
      <td>${catDisplay}</td>
      <td><span class="badge badge-green">${getCurrencySymbol()}${(p.price||0).toFixed(2)}</span></td>
      <td>${stockDisplay}</td>
      <td>${_prodStatusBadge(p)}</td>
      <td>
        <div class="prod-action-btn-row">
          <button class="prod-action-btn edit" onclick="openProdEdit(${p.id})" title="Edit">✎</button>
          <button class="prod-action-btn view" onclick="viewProduct(${p.id})" title="View">👁</button>
          <button class="prod-action-btn" onclick="viewOrdersForProduct(${p.id})" title="View orders for this product">🧾</button>
          <button class="prod-action-btn" onclick="viewCustomersForProduct(${p.id})" title="See who bought this">👥</button>
          <button class="prod-action-btn" onclick="duplicateProduct(${p.id})" title="Duplicate">⧉</button>
          <button class="prod-action-btn del"  onclick="deleteProduct(${p.id})" title="Delete">✕</button>
        </div>
      </td>
    </tr>`;
  }).join('');
}

function _renderProductGrid(products) {
  const grid = document.getElementById('products-grid');
  if (!grid) return;
  if (!products.length) {
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1;text-align:center;padding:24px;">📦 No products found<br><span style="font-size:11px;color:var(--text-dim);">Try adjusting your search or filters, or add a new product above.</span></div>';
    return;
  }
  grid.innerHTML = products.map(p => `
    <div class="product-card" style="${_selectedProductIds.has(p.id)?'border-color:var(--green);':''}" onclick="toggleProductSelect(${p.id},!_selectedProductIds.has(${p.id}))">
      <div class="product-card-img">
        ${p.image_url
          ? `<img src="${escHtml(p.image_url)}" alt="${escHtml(p.name||'')}" onerror="this.parentNode.textContent='📦'">`
          : '📦'}
      </div>
      <div class="product-card-body">
        <div class="product-card-name">${escHtml(p.name||'—')}</div>
        <div class="product-card-price">${getCurrencySymbol()}${(p.price||0).toFixed(2)}</div>
        ${window.IS_SERVICE_BUSINESS ? '' : (typeof p.stock === 'number' ? `<div style="font-size:10px;font-family:var(--mono);color:${p.stock<=5?'var(--amber)':'var(--text-dim)'};margin-top:3px;">${p.stock<=5&&p.stock>0?'⚠ Low: ':''}${p.stock === 0?'Out of stock':`${p.stock} in stock`}</div>` : '')}
        <div style="display:flex;gap:6px;margin-top:8px;">
          <button class="btn btn-ghost" style="flex:1;font-size:10px;padding:5px;" onclick="event.stopPropagation();openProdEdit(${p.id})">✎ Edit</button>
          <button class="btn btn-ghost" style="font-size:10px;padding:5px 8px;" onclick="event.stopPropagation();viewOrdersForProduct(${p.id})" title="View orders for this product">🧾</button>
          <button class="btn btn-ghost" style="font-size:10px;padding:5px 8px;" onclick="event.stopPropagation();viewCustomersForProduct(${p.id})" title="See who bought this">👥</button>
          <button class="btn btn-ghost" style="font-size:10px;padding:5px 8px;" onclick="event.stopPropagation();duplicateProduct(${p.id})" title="Duplicate">⧉</button>
          <button class="btn btn-ghost" style="font-size:10px;padding:5px 8px;" onclick="event.stopPropagation();deleteProduct(${p.id})" title="Delete">✕</button>
        </div>
      </div>
    </div>`).join('');
}

// ── IMAGE UPLOAD HELPERS ──────────────────────────────────
function handleImgSelect(event) {
  const file = event.target.files && event.target.files[0];
  _loadImageFile(file);
}

function handleImgDrop(event) {
  event.preventDefault();
  const area = document.getElementById('img-upload-area');
  if (area) area.classList.remove('drag');
  const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
  _loadImageFile(file);
}

let _pendingImgFile = null;  // raw File object for direct upload

function _loadImageFile(file) {
  if (!file) return;
  if (!file.type.startsWith('image/')) { toast('Please select an image file', true); return; }
  if (file.size > 5 * 1024 * 1024) { toast('Image must be under 5MB', true); return; }
  _pendingImgFile = file;  // store raw file for backend upload
  const reader = new FileReader();
  // Show progress bar during file read
  const progressWrap = document.getElementById('img-progress-wrap');
  const progressBar  = document.getElementById('img-progress-bar');
  if (progressWrap) progressWrap.style.display = '';
  if (progressBar)  { progressBar.style.width = '0'; setTimeout(() => { progressBar.style.width = '60%'; }, 50); }

  reader.onload = (e) => {
    _pendingImgDataUrl = e.target.result;
    const preview   = document.getElementById('img-preview');
    const clearBtn  = document.getElementById('img-clear-btn');
    const actionBar = document.getElementById('img-action-bar');
    const area      = document.getElementById('img-upload-area');
    if (progressBar) progressBar.style.width = '100%';
    setTimeout(() => { if (progressWrap) progressWrap.style.display = 'none'; }, 500);
    if (preview)   { preview.src = _pendingImgDataUrl; preview.classList.add('show'); }
    if (clearBtn)  clearBtn.style.display = 'inline';
    if (actionBar) actionBar.style.display = 'flex';
    if (area)      area.style.display = 'none';
  };
  reader.readAsDataURL(file);
}

function clearProductImg() {
  _pendingImgDataUrl = null;
  _pendingImgFile    = null;
  const preview  = document.getElementById('img-preview');
  const clearBtn = document.getElementById('img-clear-btn');
  const actionBar= document.getElementById('img-action-bar');
  const area     = document.getElementById('img-upload-area');
  const fileInput= document.getElementById('product-img-file');
  const progress = document.getElementById('img-progress-wrap');
  if (preview)   { preview.src = ''; preview.classList.remove('show'); }
  if (clearBtn)  clearBtn.style.display = 'none';
  if (actionBar) actionBar.style.display = 'none';
  if (area)      area.style.display = '';
  if (fileInput) fileInput.value = '';
  if (progress)  progress.style.display = 'none';
}

async function addProduct() {
  const nameEl  = document.getElementById('product-name');
  const priceEl = document.getElementById('product-price');
  const stockEl = document.getElementById('product-stock');
  const descEl  = document.getElementById('product-description');
  const catEl   = document.getElementById('product-category');
  const name  = nameEl  ? nameEl.value.trim()       : '';
  const price = priceEl ? parseFloat(priceEl.value) : NaN;
  if (!name || isNaN(price) || price <= 0) { toast('Enter a valid name and price', true); return; }
  const btn = document.getElementById('add-product-btn');
  try {
    if (btn) { btn.disabled = true; btn.textContent = 'Adding…'; }
    const payload = { name, price };
    if (_pendingImgFile || _pendingImgDataUrl) {
      if (btn) btn.textContent = 'Uploading image…';
      if (_pendingImgFile) {
        // Direct file upload — faster than base64 round-trip
        try {
          const fd = new FormData();
          fd.append('file', _pendingImgFile);
          const upResp = await fetch(API + '/products/upload-image', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` },
            body: fd,
          });
          if (upResp.ok) {
            const upData = await upResp.json();
            payload.image_url = upData.url;
          } else {
            console.warn('Upload failed, using data URL fallback');
            payload.image_url = _pendingImgDataUrl;
          }
        } catch (upErr) {
          console.warn('Upload error:', upErr);
          payload.image_url = _pendingImgDataUrl;
        }
      } else {
        // Fallback: convert base64 data URL via uploadImageToSupabase
        const imgUrl = await uploadImageToSupabase(
          _pendingImgDataUrl,
          name.toLowerCase().replace(/[^a-z0-9]/g, '_') + '_' + Date.now()
        );
        payload.image_url = imgUrl;
      }
    }
    if (stockEl && stockEl.value !== '') payload.stock = parseInt(stockEl.value, 10);
    if (descEl  && descEl.value.trim())  payload.description  = descEl.value.trim();
    if (catEl   && catEl.value)          payload.category     = catEl.value;
    await apiFetch(ROUTES.products, { method: 'POST', body: JSON.stringify(payload) });
    if (nameEl)  nameEl.value  = '';
    if (priceEl) priceEl.value = '';
    if (stockEl) stockEl.value = '';
    if (descEl)  descEl.value  = '';
    if (catEl)   catEl.value   = '';
    clearProductImg();
    toast(`✅ ${name} added`);
    loadProducts();
  } catch(e) { toast(e.message || 'Failed to add product', true); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '+ Add to Menu'; } }
}

// UI/UX audit: this used to delete the product immediately on click, with
// no confirmation of any kind — one misclick permanently removed a product
// from the catalog. Reuses the existing bulk-action confirm modal (built
// for bulk delete/activate/deactivate) rather than building a second one.
function deleteProduct(id) {
  const p = _allProducts.find(x => x.id === id);
  const modal    = document.getElementById('prod-bulk-modal');
  const titleEl  = document.getElementById('bulk-modal-title');
  const msgEl    = document.getElementById('bulk-modal-msg');
  const confirmBtn = document.getElementById('bulk-modal-confirm');
  if (!modal) { _deleteProductConfirmed(id); return; } // fallback if modal missing
  if (titleEl) titleEl.textContent = 'Delete Product?';
  if (msgEl)   msgEl.textContent = `This will remove "${(p && p.name) || 'this product'}" from your catalog. This cannot be undone.`;
  if (confirmBtn) confirmBtn.onclick = () => { closeBulkModal(); _deleteProductConfirmed(id); };
  modal.style.display = 'flex';
}

async function _deleteProductConfirmed(id) {
  try {
    await apiFetch(`${ROUTES.products}/${id}`, { method: 'DELETE' });
    toast('🗑 Product removed');
    loadProducts();
  } catch(e) { toast('Failed to remove product', true); }
}

// UI/UX audit: no "Duplicate" action existed — useful for near-identical
// products (size/flavor variants). Reuses the same create endpoint addProduct()
// already uses, just pre-filled from the existing product.
async function duplicateProduct(id) {
  const p = _allProducts.find(x => x.id === id);
  if (!p) { toast('Product not found', true); return; }
  const payload = {
    name: `${p.name || 'Product'} (Copy)`,
    price: p.price || 0,
  };
  if (typeof p.stock === 'number') payload.stock = p.stock;
  if (typeof p.low_stock_threshold === 'number') payload.low_stock_threshold = p.low_stock_threshold;
  if (p.description) payload.description = p.description;
  if (p.category)    payload.category    = p.category;
  if (p.image_url)   payload.image_url   = p.image_url;
  try {
    await apiFetch(ROUTES.products, { method: 'POST', body: JSON.stringify(payload) });
    toast(`✅ Duplicated "${p.name}"`);
    loadProducts();
  } catch (e) { toast('Failed to duplicate: ' + e.message, true); }
}

// ── CONVERSATIONS ─────────────────────────────────────────
// UI/UX audit: the conversation list had no search at all and the backend
// /chat/conversations endpoint returns everything with no `search` param,
// so this filters client-side over the already-loaded list — same pattern
// as filterOrdersBySearch/filterCrmTable, no backend change needed.
let _convListData  = [];
let _convVisible    = [];
let _convSearchQuery = '';

async function loadConversations() {
  try {
    const convos = await apiFetch(ROUTES.conversations);
    if (!convos) return;
    _convListData = Array.isArray(convos) ? convos : (convos.data || []);
    // stat-customers is now populated from /crm/segments (loadCustomerStats)
    // for consistency with the Customers tab — do not overwrite it here.
    _renderConversationList(_applyConvFilter());
  } catch(e){ const _cl=document.getElementById('contact-list'); if(_cl) _cl.innerHTML=_errorBlock('conversations', 'loadConversations'); }
}

function _applyConvFilter() {
  if (!_convSearchQuery) return _convListData;
  const q = _convSearchQuery;
  return _convListData.filter(c => {
    const phone = (c.phone || c.customer_phone || '').toLowerCase();
    const name  = (c.customer_name || c.name || '').toLowerCase();
    return phone.includes(q) || name.includes(q);
  });
}

// Debounced: this only ever runs from the search box's oninput (see
// _debounce's comment above) — no other code calls it expecting an
// immediate render, unlike filterOrdersBySearch which deep-links from
// several places and must stay synchronous.
const filterConversations = _debounce(function(query) {
  _convSearchQuery = (query || '').trim().toLowerCase();
  _renderConversationList(_applyConvFilter());
}, 150);

function _renderConversationList(list_data) {
  _convVisible = list_data;
  const list = document.getElementById('contact-list');
  if (!list) return;
  if (!list_data.length) {
    const msg = _convSearchQuery
      ? 'No conversations match this search.'
      : 'No conversations yet.<br><span style="font-size:11px;color:var(--text-dim);">Customers who message your WhatsApp number will show up here.</span>';
    list.innerHTML = `<div class="empty">${msg}</div>`;
    return;
  }
  list.innerHTML=list_data.map(c=>{
    const phone = c.phone || c.customer_phone || '—';
    const lastMsg = c.last_message || c.message || '';
    const lastDir = c.last_direction || c.direction || '';
    const lastAt = c.last_message_at || c.created_at || c.timestamp || null;
    const unread = c.unread_count || 0;
    return `<div class="contact-item ${phone===activePhone?'active':''}" onclick="openChat('${escHtml(phone)}',this)">
    <div class="contact-phone">${escHtml(phone)}${unread>0?` <span class="badge badge-green">${unread}</span>`:''}</div>
    <div class="contact-preview">${lastDir==='incoming'||lastDir==='in'?'👤':'🤖'} ${escHtml(formatMessagePreview(lastMsg))}</div>
    <div class="contact-time">${fmtTime(lastAt)}</div>
  </div>`;
  }).join('');
}

async function openChat(phone, el) {
  if (!phone) return;
  activePhone = phone;
  document.querySelectorAll('.contact-item').forEach(i => i.classList.remove('active'));
  if (el) el.classList.add('active');

  const win = document.getElementById('chat-window');
  if (!win) return;

  const phoneEsc = escHtml(phone).replace(/'/g,"\\'");
  win.innerHTML = `
    <div class="chat-header" style="display:flex;align-items:center;flex-wrap:wrap;row-gap:6px;">
      Chat with <span style="margin-left:6px;">${escHtml(phone)}</span>
      <span id="conv-context-inline" style="margin-left:10px;font-family:var(--mono);font-size:10px;color:var(--text-dim);"></span>
      <!-- UI/UX audit (Phase 10): three header buttons plus the phone/context
           text with no flex-wrap would clip on a narrow mobile chat pane
           (.chat-layout has overflow:hidden) instead of dropping to a new
           row — wrap + margin-left:auto keeps them right-aligned on wide
           screens and lets them fall to their own row on small ones. -->
      <div style="margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;">
        <button class="panel-action" style="font-size:11px;" onclick="viewCustomerFromChat()" title="View in Customers">👤 Customer</button>
        <button class="panel-action" style="font-size:11px;" onclick="viewOrdersFromChat()" title="View this customer's orders">🛒 Orders</button>
        <button class="panel-action" style="font-size:11px;" onclick="openChat('${phoneEsc}',null)" aria-label="Refresh conversation" title="Refresh">↻</button>
      </div>
    </div>
    <div class="chat-messages" id="chat-msgs"><div class="empty">Loading...</div></div>
    <div class="chat-reply-bar" id="chat-reply-bar">
      <textarea
        class="chat-reply-input"
        id="chat-reply-input"
        placeholder="Type a reply… (Enter to send, Shift+Enter for new line)"
        rows="1"
        onkeydown="handleDashboardSendKey(event)"
        oninput="autoResizeDashboard(this)"
      ></textarea>
      <button class="chat-reply-btn" id="chat-reply-btn" onclick="sendFromDashboard().catch(()=>{})">Send ➤</button>
    </div>`;

  win.dataset.activePhone = phone;
  await loadChatMessages(phone);
  loadConvContextInline(phone);
}

// UI/UX audit: Conversations had no customer-context info and no link back
// to CRM/Orders at all — this small inline summary + the two header buttons
// close that gap. Looks up the customer from whatever CRM data is already
// cached (populated by the Customers tab) rather than issuing a parallel
// fetch on every chat open; if it's not cached yet, fetches /chat/customers
// once, same endpoint sendFromDashboard() already relies on.
async function loadConvContextInline(phone) {
  const el = document.getElementById('conv-context-inline');
  if (!el) return;
  try {
    let match = (_crmTableData || []).find(c => c.phone === phone);
    if (!match) {
      const customers = await apiFetch('/chat/customers');
      match = (Array.isArray(customers) ? customers : []).find(c => c.phone === phone);
    }
    if (document.getElementById('conv-context-inline') !== el) return; // chat switched while awaiting
    if (!match) { el.textContent = ''; return; }
    const seg = getSegmentLabel(match.order_count, match.total_spent);
    el.textContent = `${seg.label} · ${match.order_count || 0} orders · ${getCurrencySymbol()}${parseFloat(match.total_spent || 0).toFixed(2)}`;
  } catch (e) { /* non-critical context line — fail silently */ }
}

// Reuses the existing CRM customer drawer rather than building a second one.
function viewCustomerFromChat() {
  const win = document.getElementById('chat-window');
  const phone = win && win.dataset.activePhone;
  if (!phone) return;
  showSection('crm', null);
  const openWhenReady = () => {
    const match = (_crmTableData || []).find(c => c.phone === phone);
    if (match) { openCustomerDrawer(match); }
    else { toast('No CRM record for ' + phone + ' yet', true); }
  };
  setTimeout(openWhenReady, 400);
}

// Reuses the Orders search box (same pattern as viewOrdersForDrawer).
function viewOrdersFromChat() {
  const win = document.getElementById('chat-window');
  const phone = win && win.dataset.activePhone;
  if (!phone) return;
  showSection('orders', null);
  setTimeout(() => {
    const search = document.getElementById('order-search');
    if (search) { search.value = phone; filterOrdersBySearch(phone); }
  }, 300);
}

async function loadChatMessages(phone) {
  const msgsEl = document.getElementById('chat-msgs');
  if (!msgsEl) return;

  // This function fully re-renders the message list on every call,
  // including the periodic background refresh added above — so a user
  // who scrolled up to read older messages must not get yanked back to
  // the bottom every 30s. Capture where they were first; only force the
  // scroll back down if they were already at/near the bottom (i.e. they
  // were following the live conversation, not reading history).
  const wasNearBottom =
    (msgsEl.scrollHeight - msgsEl.scrollTop - msgsEl.clientHeight) < 120;
  const prevScrollTop    = msgsEl.scrollTop;
  const prevScrollHeight = msgsEl.scrollHeight;
  const hadContent       = msgsEl.dataset.loaded === '1';

  try {
    const raw = await apiFetch(`${ROUTES.conversations}/${encodeURIComponent(phone)}`);
    if (!raw) return;
    const msg_list = Array.isArray(raw) ? raw : (Array.isArray(raw.messages) ? raw.messages : []);
    if (!msg_list.length) { msgsEl.innerHTML = '<div class="empty">No messages yet.</div>'; return; }
    msgsEl.innerHTML = msg_list.map(m => {
      const text  = m.message || m.text || '';
      const dir   = m.direction || '';
      const isBc  = text.startsWith('[BROADCAST]');
      const isOut = dir === 'outgoing' || dir === 'out';
      const cls   = isBc ? 'msg-broadcast' : `msg-${isOut ? 'out' : 'in'}`;
      const txt   = isBc ? '📢 ' + escHtml(text.replace('[BROADCAST] ', '')) : escHtml(text);
      return `<div class="msg ${cls}">${txt}<div class="msg-time">${fmtTime(m.created_at || m.createdAt || m.timestamp)}</div></div>`;
    }).join('');
    msgsEl.dataset.loaded = '1';

    if (!hadContent || wasNearBottom) {
      msgsEl.scrollTop = msgsEl.scrollHeight;
    } else {
      // New messages (if any) were appended at the end, so the growth in
      // scrollHeight is content added below the user's current view —
      // hold their position steady relative to the top, same compensation
      // used for the Live Inbox's older-message pagination.
      msgsEl.scrollTop = prevScrollTop + (msgsEl.scrollHeight - prevScrollHeight);
    }
  } catch(e) {
    const el = document.getElementById('chat-msgs');
    if (el) el.innerHTML = `<div class="empty">⚠ ${e.message}</div>`;
  }
}

async function sendFromDashboard() {
  const win   = document.getElementById('chat-window');
  const input = document.getElementById('chat-reply-input');
  const btn   = document.getElementById('chat-reply-btn');
  if (!win || !input || !btn) return;

  const phone = win.dataset.activePhone;
  const text  = input.value.trim();
  if (!phone || !text) return;

  btn.disabled = true;
  input.value  = '';
  autoResizeDashboard(input);

  try {
    const crmList = await apiFetch('/chat/customers');
    const customers = Array.isArray(crmList) ? crmList : [];
    const customer  = customers.find(cu => cu.phone === phone);
    if (!customer) { toast('Customer not found — have they messaged you first?', true); return; }

    const sendRes = await apiFetch('/chat/send', {
      method: 'POST',
      body: JSON.stringify({ customer_id: customer.id, text }),
    });

    // FIX: surface WhatsApp delivery errors so the agent knows the message
    // was saved but may not have been delivered via WhatsApp.
    if (sendRes && sendRes.whatsapp_result && sendRes.whatsapp_result.error) {
      toast('⚠ Saved but WhatsApp delivery may have failed: ' + sendRes.whatsapp_result.error, true);
    } else {
      toast('✅ Sent');
    }
    await loadChatMessages(phone);
  } catch(e) {
    toast('Send failed: ' + e.message, true);
    if (input) input.value = text;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function handleDashboardSendKey(e) {
  if (!e) return;
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendFromDashboard().catch(() => {});
  }
}

function autoResizeDashboard(el) {
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 100) + 'px';
}

// ── BROADCAST ─────────────────────────────────────────────
// Broadcast state
let allCustomerData = [];
let selectedPhones  = new Set();
let lastBroadcastAt = null;

async function loadCustomers() {
  try {
    const data = await apiFetch(ROUTES.customers);
    if (!data) return;
    const phones = Array.isArray(data.phones) ? data.phones.filter(Boolean)
      : Array.isArray(data) ? data.filter(Boolean) : [];
    customerPhones = phones;

    // Try to enrich with order count from analytics
    try {
      const top = await apiFetch('/analytics/top-customers?limit=500');
      const topMap = {};
      (Array.isArray(top) ? top : []).forEach(c => { topMap[c.phone] = c; });
      allCustomerData = phones.map(p => ({
        phone:       p,
        order_count: (topMap[p] || {}).order_count || 0,
        total_spent: (topMap[p] || {}).total_spent || 0,
        last_seen:   (topMap[p] || {}).last_seen   || null,
      }));
    } catch (_) {
      allCustomerData = phones.map(p => ({ phone: p, order_count: 0, total_spent: 0, last_seen: null }));
    }

    selectedPhones = new Set(phones);
    applyRecipientFilter();
    const statTotal = document.getElementById('stat-total');
    if (statTotal) statTotal.textContent = phones.length;
  } catch (e) {
    const rl = document.getElementById('recipient-list');
    if (rl) rl.innerHTML = `<div class="empty">⚠ ${e.message}</div>`;
  }
}

function applyRecipientFilter() {
  const filter = (document.getElementById('recipient-filter') || {}).value || 'all';
  const now = Date.now();
  let filtered = allCustomerData;
  switch (filter) {
    case 'recent':
      filtered = allCustomerData.filter(c => c.last_seen && (now - new Date(c.last_seen).getTime()) < 7*24*3600*1000);
      break;
    case 'ordered':
      filtered = allCustomerData.filter(c => c.order_count >= 1);
      break;
    case 'top':
      filtered = allCustomerData.filter(c => c.order_count >= 3);
      break;
    case 'pending_payment':
      filtered = allCustomerData.filter(c => c.order_count >= 1);
      break;
    default:
      filtered = allCustomerData;
  }
  if (filter !== 'custom') selectedPhones = new Set(filtered.map(c => c.phone));
  renderRecipientList(filtered, filter === 'custom');
  updateBroadcastStats();
}

function renderRecipientList(customers, showCheckboxes) {
  const rl = document.getElementById('recipient-list');
  if (!rl) return;
  if (!customers.length) {
    rl.innerHTML = '<div class="empty" style="padding:10px;font-family:var(--mono);font-size:12px;color:var(--text-dim);">No customers match this filter.</div>';
    updateBroadcastStats();
    return;
  }
  const items = customers.map(c => {
    const isChecked = selectedPhones.has(c.phone);
    const label = c.order_count > 0 ? ` · ${c.order_count} orders` : '';
    if (showCheckboxes) {
      return `<label style="display:flex;align-items:center;gap:8px;padding:6px 4px;cursor:pointer;border-bottom:1px solid var(--border);">
        <input type="checkbox" ${isChecked ? 'checked' : ''} data-phone="${escHtml(c.phone)}"
          onchange="toggleRecipient('${escHtml(c.phone)}',this.checked)"
          style="accent-color:var(--green);cursor:pointer;width:14px;height:14px;flex-shrink:0;"/>
        <span style="font-family:var(--mono);font-size:11px;color:var(--text-dim);">📱 ${escHtml(c.phone)}${label}</span>
      </label>`;
    }
    return `<span style="display:inline-flex;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:3px 8px;font-family:var(--mono);font-size:11px;color:var(--text-dim);margin:2px;">📱 ${escHtml(c.phone)}${label}</span>`;
  }).join('');
  rl.innerHTML = showCheckboxes ? `<div style="max-height:200px;overflow-y:auto;">${items}</div>` : `<div style="display:flex;flex-wrap:wrap;gap:4px;max-height:160px;overflow-y:auto;">${items}</div>`;
  updateBroadcastStats();
}

function toggleRecipient(phone, checked) {
  if (checked) selectedPhones.add(phone); else selectedPhones.delete(phone);
  updateBroadcastStats();
}

function toggleSelectAll() {
  const cbs = [...document.querySelectorAll('#recipient-list input[type=checkbox]')];
  const allChk = cbs.every(cb => cb.checked);
  cbs.forEach(cb => { cb.checked = !allChk; toggleRecipient(cb.dataset.phone, !allChk); });
  updateBroadcastStats();
}

function updateBroadcastStats() {
  const count = selectedPhones.size;
  const rc = document.getElementById('recipient-count');
  const ss = document.getElementById('stat-selected');
  if (rc) rc.textContent = `${count} recipient${count !== 1 ? 's' : ''} selected`;
  if (ss) ss.textContent = count;
}

function updatePreview(){
  const _bm=document.getElementById('broadcast-msg');
  const _pv=document.getElementById('preview-box');
  const _cc=document.getElementById('char-count');
  const msg=_bm?_bm.value:'';
  if(_pv) _pv.innerHTML=msg?escHtml(msg):'<span style="color:var(--text-dim);font-style:italic;">Message preview...</span>';
  if(_cc) _cc.textContent=`${msg.length} / 1024`;
}
function setTpl(t){const _bm=document.getElementById('broadcast-msg');if(_bm)_bm.value=t;updatePreview();}

async function sendBroadcast() {
  const _bm  = document.getElementById('broadcast-msg');
  const msg  = (_bm ? _bm.value : '').trim();
  const result = document.getElementById('broadcast-result');

  if (!msg) { toast('Write a message first', true); return; }
  if (!selectedPhones.size) { toast('No recipients selected', true); return; }
  if (!confirm(`Send to ${selectedPhones.size} customer${selectedPhones.size !== 1 ? 's' : ''}?

This will send a WhatsApp message to each selected recipient.`)) return;

  const btn = document.getElementById('broadcast-send-btn');
  if (btn) btn.disabled = true;
  if (result) result.style.display = 'none';

  try {
    const phones = [...selectedPhones];
    const data = await apiFetch(ROUTES.broadcast, {
      method: 'POST',
      body: JSON.stringify({
        message:     msg,
        phone_filter: phones,   // send only to selected phones
      }),
    });

    if (!data) return;
    const ok = data.failed === 0;

    // UI/UX audit — Phase 9 recommendation ("Broadcast -> reached
    // customers"): the successfully-reached phones were known client-side
    // (the selection minus whatever failed_numbers came back) but never
    // linked anywhere. Filters CRM down to just those phones via the new
    // filterCrmByPhoneSet(), reusing the existing CRM table/filter-chip UI
    // rather than building a second customer list.
    const failedSet  = new Set((data.failed_numbers || []));
    const reachedPhones = phones.filter(p => !failedSet.has(p));
    const viewReachedBtn = reachedPhones.length
      ? `<div style="margin-top:8px;"><button class="btn btn-ghost" style="font-size:11px;padding:5px 12px;" onclick='filterCrmByPhoneSet(${JSON.stringify(reachedPhones)}, "Reached by last broadcast")'>👥 View these ${reachedPhones.length} customer${reachedPhones.length!==1?'s':''}</button></div>`
      : '';

    if (result) {
      result.style.display = 'block';
      result.className = 'broadcast-result ' + (ok ? 'success' : 'error');
      result.innerHTML = (ok
        ? `✅ Sent to <strong>${data.sent}</strong> customer${data.sent !== 1 ? 's' : ''}!`
        : `Sent: <strong>${data.sent}</strong>  |  Failed: <strong>${data.failed}</strong>` +
          (data.failed_numbers && data.failed_numbers.length
            ? `<div style="font-size:10px;margin-top:6px;opacity:0.7;">Failed: ${data.failed_numbers.slice(0,5).join(', ')}${data.failed_numbers.length > 5 ? '…' : ''}</div>`
            : '')
      ) + viewReachedBtn;
    }

    if (ok) {
      toast(`📢 Broadcast sent to ${data.sent}!`);
      if (_bm) _bm.value = '';
      updatePreview();
      // Update last broadcast time
      const statLast = document.getElementById('stat-last');
      if (statLast) statLast.textContent = 'Just now';
      lastBroadcastAt = new Date();
    } else {
      toast(`Sent ${data.sent}, failed ${data.failed}`, !ok);
    }

  } catch (e) {
    if (result) { result.style.display = 'block'; result.className = 'broadcast-result error'; result.textContent = `❌ ${e.message}`; }
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── STATUS ────────────────────────────────────────────────
async function checkStatus(){
  const el=document.getElementById('api-status-text');
  const spin=document.getElementById('api-spin');
  try{
    const r=await fetch(`${API}/`);
    if(r.ok){if(el)el.textContent='API Online';if(spin)spin.style.borderTopColor='var(--green)';}
    else{if(el)el.textContent='API Error';if(spin)spin.style.borderTopColor='var(--red)';}
  } catch{if(el)el.textContent='API Offline';if(spin)spin.style.borderTopColor='var(--red)';}
}

// ── SETTINGS ──────────────────────────────────────────────
async function loadSettings() {
  // Load business profile
  try {
    const b = await apiFetch('/me');
    if (!b) return;
    _setVal('set-biz-name',      b.name || '');
    // Category: if the stored value isn't in the select options, use "Other" + custom
    const _catSel = document.getElementById('set-category');
    const _catOpts = _catSel ? Array.from(_catSel.options).map(o => o.value) : [];
    const _storedCat = b.category || '';
    if (_storedCat && !_catOpts.includes(_storedCat)) {
      _setVal('set-category', 'Other');
      _setVal('set-custom-category', _storedCat);
      const _customEl = document.getElementById('set-custom-category');
      if (_customEl) _customEl.style.display = 'block';
    } else {
      _setVal('set-category', _storedCat);
    }
    // Currency
    if (b.currency) _setVal('set-currency', b.currency);
    if (b.currency_symbol) _setVal('set-currency-symbol', b.currency_symbol);
    const _svcChk = document.getElementById('set-is-service-business');
    if (_svcChk) {
      const _sv = b.is_service_business != null
        ? !!b.is_service_business : _autoDetectServiceMode(b.category);
      _svcChk.checked = _sv;
      onBizTypeToggle(_sv, /*_skipSave=*/true);
    }
    // Cash & Currency toggles — restore saved state (was previously never
    // read back, so toggles always showed their hardcoded HTML default)
    const cashToggle   = document.getElementById('set-cash-enabled');
    const pickupToggle = document.getElementById('set-pickup-enabled');
    if (cashToggle)   cashToggle.checked   = b.cash_enabled   !== false;  // default true if unset
    if (pickupToggle) pickupToggle.checked = b.pickup_enabled !== false; // default true if unset
    _setVal('set-description',   b.description || '');
    _setVal('set-contact-phone', b.contact_phone || '');
    _setVal('set-support-email', b.support_email || '');
    _setVal('set-owner-email',   b.owner_email   || '');  // Sprint 8 Fix 5

    // Account & Security panel
    const usernameEl = document.getElementById('profile-username');
    if (usernameEl) usernameEl.textContent = b.owner_username || localStorage.getItem('wazi_user') || '—';
    const secEmailEl = document.getElementById('sec-owner-email');
    if (secEmailEl) secEmailEl.value = b.owner_email || '';
    _setVal('set-address',       b.address || '');
    _setVal('set-city',          b.city || '');
    _setVal('set-hours',         b.business_hours || '');
    _setVal('set-instagram',     b.instagram || '');
    _setVal('set-facebook',      b.facebook || '');
    // Multi-language toggle — restore saved state from features_json
    const translationToggle = document.getElementById('set-translation-enabled');
    if (translationToggle) {
      translationToggle.checked = !!(b.features_json && b.features_json.translation_enabled);
    }
    // WhatsApp Connection panel — phone_id is safe to show back; the
    // access token itself is never returned by the API (GET /me strips
    // it), so that field is intentionally left blank for the owner to
    // re-enter only if they're changing it.
    _setVal('set-wa-phone-id', b.whatsapp_phone_id || '');
    const waStatusEl = document.getElementById('wa-connection-status');
    if (waStatusEl) {
      if (b.whatsapp_phone_id) {
        waStatusEl.textContent = '● Dedicated number configured';
        waStatusEl.style.color = 'var(--green)';
      } else if (b.use_shared_number) {
        waStatusEl.textContent = '● Using WaziBot shared number';
        waStatusEl.style.color = 'var(--green)';
      } else {
        waStatusEl.textContent = '○ Not configured';
        waStatusEl.style.color = 'var(--text-dim)';
      }
    }
  } catch(e) { console.warn('loadSettings /me:', e.message); }

  // Load payment settings
  try {
    const pay = await apiFetch('/me/payment-settings');
    if (pay) {
      _setVal('set-ecocash-number', pay.ecocash_number || '');
      _setVal('set-ecocash-name',   pay.ecocash_name   || '');
      _setVal('set-paypal-email',   pay.paypal_email   || '');
      _setVal('set-banktransfer-details', pay.bank_transfer_details || '');
      _setVal('set-blik-number',    pay.blik_number    || '');
      const statusEl = document.getElementById('payment-settings-status');
      if (statusEl) {
        const parts = [];
        if (pay.ecocash_configured) parts.push('✅ EcoCash configured');
        else parts.push('⚠️ EcoCash not set');
        if (pay.paypal_configured)  parts.push('✅ PayPal configured');
        else parts.push('⚠️ PayPal not set');
        statusEl.innerHTML = parts.map(p => `<div style="color:${p.startsWith('✅')?'var(--green)':'var(--amber)'}">${p}</div>`).join('');
      }
    }
  } catch(e) { console.warn('loadSettings payments:', e.message); }

  // Load Stripe billing status
  loadStripeBillingStatus();
  loadAcquisitionStats();

  // Appearance — font: prefer Supabase (already loaded above in b.features_json),
  // fall back to localStorage, fall back to default Syne.
  const savedTheme = localStorage.getItem('wazi_theme') || 'dark';
  const _dbFont = (typeof b !== 'undefined' && b && b.features_json && b.features_json.dashboard_font)
                  ? b.features_json.dashboard_font : null;
  const savedFont = _dbFont || localStorage.getItem('wazi_font') || "'Syne',sans-serif";
  if (_dbFont) localStorage.setItem('wazi_font', _dbFont); // keep in sync
  setTheme(savedTheme, true);
  const fontSel = document.getElementById('font-select');
  if (fontSel) { fontSel.value = savedFont; applyFont(savedFont, true); }
}

// Helper: safely set an input/select/textarea value
function _setVal(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

// ── SETTINGS TAB SWITCHER ─────────────────────────────────
function switchSettingsTab(tab, btn) {
  document.querySelectorAll('.stab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.stab-content').forEach(c => c.classList.remove('active'));
  if (btn) btn.classList.add('active');
  const content = document.getElementById('stab-' + tab);
  if (content) content.classList.add('active');
}

// ── PROFILE SAVE ──────────────────────────────────────────
async function saveProfile() {
  const name = (_getVal('set-biz-name') || '').trim();
  if (!name) { toast('Business name is required', true); return; }
  const btn = document.querySelector('[onclick="saveProfile()"]');
  try {
    setLoading(btn, true);
    // Use custom category if "Other" is selected
    const catSel    = _getVal('set-category');
    const catCustom = _getVal('set-custom-category').trim();
    const category  = (catSel === 'Other' && catCustom) ? catCustom : catSel;

    // Currency symbol — use override if provided, else derive from selection
    const currSym = _getVal('set-currency-symbol').trim() ||
                    _currencySymbolFor(_getVal('set-currency'));

    // Fix 3: include owner_email — undefined is omitted by JSON.stringify so
    // an empty field sends nothing (no accidental email wipe)
    const _ownerEmail = (_getVal('set-owner-email') || '').trim() || undefined;
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({
      name,
      category,
      description:     _getVal('set-description'),
      contact_phone:   _getVal('set-contact-phone'),
      support_email:   _getVal('set-support-email'),
      address:         _getVal('set-address'),
      city:            _getVal('set-city'),
      business_hours:  _getVal('set-hours'),
      instagram:       _getVal('set-instagram'),
      facebook:        _getVal('set-facebook'),
      currency:             _getVal('set-currency'),
      currency_symbol:      currSym,
      owner_email:          _ownerEmail,
      is_service_business:  !!(document.getElementById('set-is-service-business')?.checked),
    })});
    bizName = name;
    localStorage.setItem('wazi_biz', bizName);
    const _rl = document.getElementById('sidebar-role-label');
    if (_rl && userRole !== 'superadmin') _rl.textContent = bizName;
    const hdr = document.getElementById('biz-name-header');
    if (hdr) hdr.textContent = '🟢 ' + bizName;
    toast('✅ Profile saved');
  } catch(e) { toast('Failed: ' + e.message, true); }
  finally { setLoading(btn, false); }
}

function _getVal(id) {
  const el = document.getElementById(id);
  return el ? el.value : '';
}

function toggleCustomCategory() {
  const sel = document.getElementById('set-category');
  const inp = document.getElementById('set-custom-category');
  if (!sel || !inp) return;
  inp.style.display = sel.value === 'Other' ? 'block' : 'none';
  if (sel.value !== 'Other') inp.value = '';
}

const _CURRENCY_SYMBOLS = {
  USD:'$', EUR:'€', GBP:'£', PLN:'zł', ZAR:'R', BWP:'P', NAD:'N$', ZMW:'ZK',
  KES:'KSh', UGX:'USh', TZS:'TSh', RWF:'RF', MWK:'MK', MZN:'MT', AOA:'Kz',
  GHS:'₵', NGN:'₦', XAF:'FCFA', XOF:'CFA', ETB:'Br', EGP:'E£', MAD:'DH',
  TND:'DT', AED:'د.إ', SAR:'﷼', QAR:'﷼', OMR:'﷼', KWD:'KD', INR:'₹',
  AUD:'A$', CAD:'C$', JPY:'¥', CNY:'¥', BTC:'₿', USDT:'₮',
};
function _currencySymbolFor(code) { return _CURRENCY_SYMBOLS[code] || '$'; }

// Re-render every section that displays money so a currency symbol change
// applies instantly across the dashboard without requiring a page reload.
// Safe to call anytime — each function re-fetches and re-renders its own
// section; sections not currently visible just update invisibly.
function refreshAllMoneyDisplays() {
  try { loadOrders();  } catch(_) {}
  try { loadProducts();} catch(_) {}
  try { loadCrm();     } catch(_) {}
  try { _postLoginInit && loadRepeatCustomerStat && loadRepeatCustomerStat(); } catch(_) {}
}

function updateCurrencySymbol(code) {
  const symEl = document.getElementById('set-currency-symbol');
  // Only auto-fill if the user hasn't set a custom override
  if (symEl && !symEl.dataset.userEdited) {
    symEl.value = _currencySymbolFor(code);
  }
}
// Track if user manually edits the symbol

/* ══ Fix 3: PLAN UPGRADE MODAL ══════════════════════════════════════════════
   Called by apiFetch when backend returns plan_required 403.
   Shows inline modal — does NOT logout, does NOT clear token.
════════════════════════════════════════════════════════════════════════════ */
function showPlanUpgradeModal(detail) {
  const isObj      = typeof detail === 'object' && detail !== null;
  const message    = isObj ? (detail.message    || 'This feature requires a higher plan.') : String(detail);
  const upgradeUrl = isObj ? (detail.upgrade_url || '/pricing') : '/pricing';

  let modal = document.getElementById('plan-upgrade-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'plan-upgrade-modal';
    modal.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:2000;align-items:center;justify-content:center;';
    modal.innerHTML = `
      <div style="background:var(--surface);border:1px solid rgba(245,158,11,0.5);
                  border-radius:20px;padding:40px 36px;max-width:400px;width:90%;
                  text-align:center;box-shadow:0 0 48px rgba(245,158,11,0.15);">
        <div style="font-size:44px;margin-bottom:14px;">🔒</div>
        <h2 id="pum-title" style="font-size:20px;font-weight:800;margin-bottom:10px;color:var(--text);">
          Upgrade Required
        </h2>
        <p id="pum-message" style="font-family:var(--mono,monospace);font-size:13px;
           color:var(--text-dim);line-height:1.7;margin-bottom:24px;"></p>
        <a id="pum-btn" href="/pricing"
           style="display:block;background:var(--green,#22c55e);color:#000;font-weight:800;
                  font-size:14px;padding:13px;border-radius:10px;text-decoration:none;margin-bottom:12px;">
          View Plans →
        </a>
        <button onclick="document.getElementById('plan-upgrade-modal').style.display='none'"
                style="background:transparent;border:none;cursor:pointer;
                       font-family:var(--mono,monospace);font-size:12px;color:var(--text-dim);">
          Dismiss
        </button>
      </div>`;
    document.body.appendChild(modal);
  }

  const msgEl = document.getElementById('pum-message');
  if (msgEl) msgEl.textContent = message;
  const btnEl = document.getElementById('pum-btn');
  if (btnEl) btnEl.href = upgradeUrl;

  modal.style.display = 'flex';
  toast('This feature is available on a paid plan — see /pricing');
}

document.addEventListener('DOMContentLoaded', () => {
  const symEl = document.getElementById('set-currency-symbol');
  if (symEl) symEl.addEventListener('input', () => { symEl.dataset.userEdited = '1'; });
});

// ── PAYMENT SAVES ─────────────────────────────────────────
async function saveEcoCashSettings() {
  const number = _getVal('set-ecocash-number').trim();
  const name   = _getVal('set-ecocash-name').trim();
  if (!number) { toast('Enter your EcoCash number', true); return; }
  if (number.length < 7) { toast('Include country code — e.g. +263...', true); return; }
  if (!name)   { toast('Enter the registered account name', true); return; }
  const btn = document.querySelector('[onclick="saveEcoCashSettings()"]');
  try {
    setLoading(btn, true);
    await apiFetch('/me/payment-settings/ecocash', { method: 'POST', body: JSON.stringify({ ecocash_number: number, ecocash_name: name }) });
    toast('✅ EcoCash saved');
    const statusEl = document.getElementById('payment-settings-status');
    if (statusEl) {
      const existing = statusEl.innerHTML;
      statusEl.innerHTML = existing.replace(/⚠️ EcoCash not set/g, '✅ EcoCash configured').replace(/>✅ EcoCash configured</g, ' style="color:var(--green)">✅ EcoCash configured<');
    }
  } catch(e) { toast('Failed: ' + e.message, true); }
  finally { setLoading(btn, false); }
}

async function savePayPalSettings() {
  const email = _getVal('set-paypal-email').trim().toLowerCase();
  if (!email || !email.includes('@')) { toast('Enter a valid PayPal email', true); return; }
  const btn = document.querySelector('[onclick="savePayPalSettings()"]');
  try {
    setLoading(btn, true);
    await apiFetch('/me/payment-settings/paypal', { method: 'POST', body: JSON.stringify({ paypal_email: email }) });
    toast('✅ PayPal email saved — customers will send to ' + email);
  } catch(e) { toast('Failed: ' + e.message, true); }
  finally { setLoading(btn, false); }
}

async function saveBankTransferSettings() {
  const details = _getVal('set-banktransfer-details').trim();
  if (!details) { toast('Enter your bank transfer details', true); return; }
  const btn = document.querySelector('[onclick="saveBankTransferSettings()"]');
  try {
    setLoading(btn, true);
    await apiFetch('/me/payment-settings/banktransfer', { method: 'POST', body: JSON.stringify({ bank_transfer_details: details }) });
    toast('✅ Bank transfer details saved');
  } catch(e) { toast('Failed: ' + e.message, true); }
  finally { setLoading(btn, false); }
}

async function saveBlikSettings() {
  const number = _getVal('set-blik-number').trim();
  if (!number) { toast('Enter your BLIK phone number', true); return; }
  if (number.length < 7) { toast('Include the full phone number', true); return; }
  const btn = document.querySelector('[onclick="saveBlikSettings()"]');
  try {
    setLoading(btn, true);
    await apiFetch('/me/payment-settings/blik', { method: 'POST', body: JSON.stringify({ blik_number: number }) });
    toast('✅ BLIK number saved — customers will send to ' + number);
  } catch(e) { toast('Failed: ' + e.message, true); }
  finally { setLoading(btn, false); }
}

// ── Stripe Billing ───────────────────────────────────────────────────────────
// WaziBot uses a single platform Stripe account (keys live in server env vars).
// Users never handle keys — they just see their plan status and upgrade/manage.

// ── Stripe Connect + Billing (Phases 1–4) ────────────────────────────────────

async function loadStripeBillingStatus() {
  // Load both Connect status and subscription status in parallel
  await Promise.all([loadStripeConnectStatus(), loadStripeSubscriptionStatus()]);
}

async function loadStripeConnectStatus() {
  const badge          = document.getElementById('stripe-badge');
  const connectSection = document.getElementById('stripe-connect-section');
  const connectedSection = document.getElementById('stripe-connected-section');

  try {
    const s = await apiFetch('/billing/connect/status');

    if (s && s.connected) {
      // Show connected section
      if (connectSection)   connectSection.style.display   = 'none';
      if (connectedSection) connectedSection.style.display = 'block';
      if (badge) {
        badge.textContent = '✅ Connected';
        badge.style.color = 'var(--green)';
        badge.style.background = 'rgba(0,200,83,.12)';
      }

      // Status badges
      const chargesBadge = document.getElementById('sc-charges-badge');
      const payoutsBadge = document.getElementById('sc-payouts-badge');
      const verifyBadge  = document.getElementById('sc-verify-badge');
      if (chargesBadge) {
        chargesBadge.textContent = `Charges: ${s.charges_enabled ? '✅ Enabled' : '⏳ Pending'}`;
        chargesBadge.style.color = s.charges_enabled ? 'var(--green)' : 'var(--amber)';
      }
      if (payoutsBadge) {
        payoutsBadge.textContent = `Payouts: ${s.payouts_enabled ? '✅ Enabled' : '⏳ Pending'}`;
        payoutsBadge.style.color = s.payouts_enabled ? 'var(--green)' : 'var(--amber)';
      }
      if (verifyBadge) {
        const vMap = { active:'✅ Active', pending:'⏳ Verification Pending', incomplete:'⚠ Incomplete' };
        verifyBadge.textContent = `Status: ${vMap[s.verification_status] || s.verification_status}`;
        verifyBadge.style.color = s.verification_status === 'active' ? 'var(--green)' : 'var(--amber)';
      }

      // Load payment analytics
      loadStripeAnalytics();

    } else {
      // Not connected — show connect prompt
      if (connectSection)   connectSection.style.display   = 'block';
      if (connectedSection) connectedSection.style.display = 'none';
      if (badge) {
        badge.textContent = '⚡ Not Connected';
        badge.style.color = 'var(--amber)';
      }
    }
  } catch(e) {
    if (badge) { badge.textContent = '⚙ Setup Required'; badge.style.color = 'var(--text-dim)'; }
    if (connectSection) connectSection.style.display = 'block';
  }
}

async function loadStripeAnalytics() {
  try {
    const a = await apiFetch('/billing/analytics');
    if (!a) return;
    const sym = (window._bizCurrencySym || '$');
    const _el = (id, val) => { const e = document.getElementById(id); if(e) e.textContent = val; };
    // Show available + pending balance split
    const available = (a.available_balance || 0).toFixed(2);
    const pending   = (a.pending_balance   || 0).toFixed(2);
    const totalDisp = pending > 0
      ? `${sym}${available} + ${sym}${pending} pending`
      : `${sym}${(a.total_revenue||0).toFixed(2)}`;
    _el('sc-total-revenue', totalDisp);
    _el('sc-orders-paid',   a.orders_paid || '0');
    _el('sc-last-payment',  a.last_payment ? new Date(a.last_payment*1000).toLocaleDateString() : '—');
    _el('sc-last-payout',   a.last_payout  ? new Date(a.last_payout*1000).toLocaleDateString()  : '—');
  } catch(e) { /* analytics unavailable — silently skip */ }
}

async function loadStripeSubscriptionStatus() {
  const planLabel  = document.getElementById('stripe-plan-label');
  const statusEl   = document.getElementById('stripe-billing-status');
  const trialEl    = document.getElementById('stripe-trial-info');
  const upgradeBtn = document.getElementById('stripe-upgrade-btn');
  const manageBtn  = document.getElementById('stripe-manage-btn');
  const cancelBtn  = document.getElementById('stripe-cancel-btn');
  try {
    const s = await apiFetch('/billing/status');
    if (!s) return;
    const tier   = (s.tier || 'free').charAt(0).toUpperCase() + (s.tier || 'free').slice(1);
    const status = s.billing_status || 'active';
    const statusColors = { active:'var(--green)', trialing:'var(--green)', past_due:'#ff5252', canceled:'var(--text-dim)' };
    if (planLabel) { planLabel.textContent = `${tier} Plan`; planLabel.style.color = statusColors[status] || 'var(--text)'; }
    if (statusEl)  { statusEl.textContent  = status.charAt(0).toUpperCase()+status.slice(1).replace('_',' '); statusEl.style.color = statusColors[status] || 'var(--text-dim)'; }
    if (trialEl) {
      if (status === 'trialing' && s.trial_ends_at) {
        const days = Math.max(0, Math.ceil((new Date(s.trial_ends_at) - Date.now()) / 86400000));
        trialEl.style.display = 'block';
        trialEl.textContent = days > 0
          ? `✅ ${days} trial day${days!==1?'s':''} remaining. Upgrade from $5.99/mo when ready.`
          : `Trial ended. Upgrade from $5.99/month to restore full access.`;
      } else { trialEl.style.display = 'none'; }
    }
    const canUpgrade   = s.tier === 'free' || status === 'trialing' || status === 'canceled';
    const hasActiveSub = s.stripe_subscription_id && (status === 'active' || status === 'past_due');
    if (upgradeBtn) upgradeBtn.style.display = canUpgrade   ? 'inline-flex' : 'none';
    if (manageBtn)  manageBtn.style.display  = hasActiveSub ? 'inline-flex' : 'none';
    if (cancelBtn)  cancelBtn.style.display  = hasActiveSub ? 'inline-flex' : 'none';
  } catch(e) { /* subscription status unavailable */ }
}

async function stripeConnect() {
  const btn = document.getElementById('stripe-connect-btn');
  const statusEl = document.getElementById('stripe-action-status');
  try {
    if (btn) { btn.disabled = true; btn.textContent = 'Opening Stripe…'; }
    const result = await apiFetch('/billing/connect', { method: 'POST' });
    if (result && result.url) {
      window.location.href = result.url;  // Stripe onboarding — full redirect
    } else {
      if (statusEl) statusEl.innerHTML = '<span style="color:var(--amber)">⚠️ ' + (result?.error || 'Could not start Stripe setup') + '</span>';
    }
  } catch(e) {
    if (statusEl) statusEl.innerHTML = `<span style="color:#ff5252">Failed: ${e.message}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⚡ Connect Stripe Account'; }
  }
}

async function stripeConnectDashboard() {
  const btn = document.getElementById('stripe-express-btn');
  const statusEl = document.getElementById('stripe-action-status');
  try {
    if (btn) { btn.disabled = true; btn.textContent = 'Opening…'; }
    const result = await apiFetch('/billing/connect/dashboard', { method: 'POST' });
    if (result && result.url) {
      window.open(result.url, '_blank');
    } else {
      if (statusEl) statusEl.innerHTML = '<span style="color:var(--amber)">⚠️ ' + (result?.error || 'Dashboard not available') + '</span>';
    }
  } catch(e) {
    if (statusEl) statusEl.innerHTML = `<span style="color:#ff5252">Failed: ${e.message}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '📊 Open Stripe Dashboard'; }
  }
}

async function stripeUpgrade(plan, period) {
  // If plan specified, go directly to Stripe Checkout. Otherwise show pricing page.
  if (plan) {
    await _launchStripeCheckout(plan, period || 'monthly');
  } else {
    window.location.href = '/static/pricing.html';
  }
}

// Central Stripe checkout launcher — used by all upgrade buttons in the app
async function _launchStripeCheckout(tier, billingPeriod) {
  billingPeriod = billingPeriod || 'monthly';
  const btn = document.querySelector('[data-checkout-loading]');
  try {
    const resp = await apiFetch('/billing/checkout', {
      method: 'POST',
      body: JSON.stringify({ tier, billing_period: billingPeriod, country_code: '' })
    });
    if (resp && resp.url) {
      window.location.href = resp.url;
    } else {
      toast((resp && resp.detail) || 'Could not start checkout — please try again.', true);
    }
  } catch(e) {
    // If not logged in, send to pricing/signup
    window.location.href = '/static/pricing.html';
  }
}

async function stripeManage() {
  // Create a Stripe customer portal session and redirect
  const btn = document.getElementById('stripe-manage-btn');
  const statusEl = document.getElementById('stripe-action-status');
  try {
    if (btn) { btn.disabled = true; btn.textContent = 'Opening…'; }
    const result = await apiFetch('/billing/portal', { method: 'POST' });
    if (result && result.url) {
      window.open(result.url, '_blank');
    } else {
      if (statusEl) statusEl.innerHTML = '<span style="color:var(--amber)">⚠️ Portal not available — contact support.</span>';
    }
  } catch(e) {
    if (statusEl) statusEl.innerHTML = `<span style="color:#ff5252">Failed: ${e.message}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⚙ Manage Subscription'; }
  }
}

async function stripeCancel() {
  if (!confirm('Cancel your subscription? You keep access until the end of your billing period.')) return;
  const btn = document.getElementById('stripe-cancel-btn');
  const statusEl = document.getElementById('stripe-action-status');
  try {
    if (btn) { btn.disabled = true; btn.textContent = 'Cancelling…'; }
    await apiFetch('/billing/cancel', { method: 'POST', body: JSON.stringify({ confirm: true }) });
    if (statusEl) statusEl.innerHTML = '<span style="color:var(--green)">✅ Subscription cancelled — access continues until period end.</span>';
    setTimeout(loadStripeBillingStatus, 1500);
  } catch(e) {
    if (statusEl) statusEl.innerHTML = `<span style="color:#ff5252">Failed: ${e.message}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Cancel Plan'; }
  }
}

async function savePaymentOptions() {
  const btn = document.querySelector('[onclick="savePaymentOptions()"]');
  const currency = _getVal('set-currency');
  if (!currency) { toast('Select a currency', true); return; }
  const newSymbol = _getVal('set-currency-symbol') || _currencySymbolFor(currency);
  try {
    setLoading(btn, true);
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({
      currency:        currency,
      currency_symbol: newSymbol || undefined,
      cash_enabled:    document.getElementById('set-cash-enabled')?.checked,
      pickup_enabled:  document.getElementById('set-pickup-enabled')?.checked,
    })});
    toast('✅ Payment options saved');
    invalidateMeCache();   // force next /me read to reflect the new values everywhere
    window.CURRENT_CURRENCY_SYMBOL = newSymbol;   // apply immediately, no reload needed
    refreshAllMoneyDisplays();
  } catch(e) { toast('Failed: ' + e.message, true); }
  finally { setLoading(btn, false); }
}

// ── WHATSAPP CONNECTION (Settings) ─────────────────────────────────────────
// Fixes a real support issue: the only place these fields were ever
// collected was the one-time onboarding wizard's "Connect My Own WhatsApp
// Number" step, with no way to view or correct them afterwards — which is
// how a mistyped or wrong value (e.g. pasting the webhook VERIFY_TOKEN
// here instead of the real Meta access token) could go unnoticed until a
// feature like Broadcast that needs it actually failed. Reuses the
// existing, already-working PATCH /business/me and GET /business/me/
// test-whatsapp endpoints — no backend change needed.
async function saveWhatsAppConnection() {
  const btn = document.querySelector('[onclick="saveWhatsAppConnection()"]');
  const resultEl = document.getElementById('wa-connection-result');
  const phoneId = _getVal('set-wa-phone-id').trim();
  const token   = _getVal('set-wa-token').trim();
  if (!phoneId) { toast('Enter your WhatsApp Phone Number ID', true); return; }
  try {
    setLoading(btn, true);
    if (resultEl) resultEl.textContent = '';
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({
      whatsapp_phone_id: phoneId,
      whatsapp_token:    token || undefined,   // blank = keep the token already saved
    })});
    toast('✅ WhatsApp connection saved');
    invalidateMeCache();
    _setVal('set-wa-token', '');   // never leave a token sitting in the input after save
    await testWhatsAppConnection();
  } catch(e) { toast('Failed: ' + e.message, true); }
  finally { setLoading(btn, false); }
}

async function testWhatsAppConnection() {
  const resultEl = document.getElementById('wa-connection-result');
  if (resultEl) resultEl.textContent = 'Testing…';
  try {
    const res = await apiFetch('/me/test-whatsapp');
    if (!resultEl) return;
    if (res && res.ok) {
      resultEl.innerHTML = `<span style="color:var(--green)">✅ ${escHtml(res.reason || 'Connected')}</span>`;
    } else {
      resultEl.innerHTML = `<span style="color:#ff5252">❌ ${escHtml((res && res.reason) || 'Connection failed')}</span>`;
    }
  } catch(e) {
    if (resultEl) resultEl.innerHTML = `<span style="color:#ff5252">❌ ${escHtml(e.message)}</span>`;
  }
}

// ── CURRENCY CONVERSION (Convert My Prices) ───────────────────────────────
// "from" currency = the business's currently SAVED currency (fetched fresh
// from /me, not the dropdown — the owner may have changed the dropdown
// without saving yet). "to" currency = whatever is currently selected in
// the dropdown right now. This avoids any ambiguity about direction.
let _ccmPreviewData = null;   // {rate, from_currency, to_currency, items[]}

async function openCurrencyConvertModal() {
  const toCurrency = _getVal('set-currency');
  if (!toCurrency) { toast('Select a target currency first', true); return; }

  invalidateMeCache();
  const biz = await getCachedMe().catch(() => null);
  const fromCurrency = (biz && biz.currency) || 'USD';

  if (fromCurrency === toCurrency) {
    toast('Pick a different currency to convert to', true);
    return;
  }

  // Reset modal to step 1 every time it opens
  document.getElementById('ccm-step-select').style.display  = 'block';
  document.getElementById('ccm-step-preview').style.display = 'none';
  document.getElementById('ccm-step-result').style.display  = 'none';
  document.getElementById('ccm-from-label').textContent = fromCurrency;
  document.getElementById('ccm-to-label').textContent   = toCurrency;
  _ccmPreviewData = null;

  document.getElementById('currency-convert-modal').classList.add('open');
}

function closeCurrencyConvertModal() {
  document.getElementById('currency-convert-modal').classList.remove('open');
}

async function loadCurrencyConvertPreview() {
  const fromCurrency = document.getElementById('ccm-from-label').textContent;
  const toCurrency   = document.getElementById('ccm-to-label').textContent;
  const btn = document.getElementById('ccm-preview-btn');

  try {
    setLoading(btn, true);
    const res = await apiFetch('/products/convert-currency/preview', {
      method: 'POST',
      body: JSON.stringify({ from_currency: fromCurrency, to_currency: toCurrency }),
    });
    _ccmPreviewData = res;

    if (!res.items || !res.items.length) {
      document.getElementById('ccm-preview-list').innerHTML =
        '<div style="padding:16px;font-family:var(--mono);font-size:12px;color:var(--text-dim);">No products to convert yet.</div>';
    } else {
      document.getElementById('ccm-preview-list').innerHTML = res.items.map(it => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:12px;">
          <span>${escHtml(it.name || ('Product #' + it.id))}</span>
          <span>
            <span style="color:var(--text-dim);text-decoration:line-through;">${fromCurrency} ${it.old_price.toFixed(2)}</span>
            <span style="margin:0 6px;color:var(--text-dim);">→</span>
            <span style="color:var(--green);font-weight:700;">${toCurrency} ${it.new_price.toFixed(2)}</span>
          </span>
        </div>`).join('');
    }

    document.getElementById('ccm-rate-from').textContent  = fromCurrency;
    document.getElementById('ccm-rate-to').textContent    = toCurrency;
    document.getElementById('ccm-rate-value').textContent = res.rate.toFixed(4);

    document.getElementById('ccm-step-select').style.display  = 'none';
    document.getElementById('ccm-step-preview').style.display = 'block';
  } catch (e) {
    toast('Could not fetch exchange rate: ' + e.message, true);
  } finally {
    setLoading(btn, false);
  }
}

async function confirmCurrencyConversion() {
  if (!_ccmPreviewData) { toast('Preview expired — please try again', true); return; }
  const btn = document.getElementById('ccm-confirm-btn');

  if (!confirm(
    `This will update the price of ${_ccmPreviewData.items.length} product(s) ` +
    `from ${_ccmPreviewData.from_currency} to ${_ccmPreviewData.to_currency}. Continue?`
  )) return;

  try {
    setLoading(btn, true);
    const res = await apiFetch('/products/convert-currency/apply', {
      method: 'POST',
      body: JSON.stringify({
        from_currency: _ccmPreviewData.from_currency,
        to_currency:   _ccmPreviewData.to_currency,
        rate:          _ccmPreviewData.rate,
      }),
    });

    const newSymbol = _currencySymbolFor(res.to_currency);
    window.CURRENT_CURRENCY_SYMBOL = newSymbol;
    invalidateMeCache();

    const resultEl = document.getElementById('ccm-result-message');
    resultEl.innerHTML = res.failed_count > 0
      ? `✅ Updated ${res.updated_count} product${res.updated_count !== 1 ? 's' : ''}. ` +
        `⚠️ ${res.failed_count} could not be updated — please check those manually.`
      : `✅ All ${res.updated_count} product price${res.updated_count !== 1 ? 's' : ''} updated to ${res.to_currency}.`;

    document.getElementById('ccm-step-preview').style.display = 'none';
    document.getElementById('ccm-step-result').style.display  = 'block';

    toast(`✅ Prices converted to ${res.to_currency}`);
    refreshAllMoneyDisplays();
    loadSettings();   // refresh currency dropdown/symbol display to match saved state
  } catch (e) {
    toast('Conversion failed: ' + e.message, true);
  } finally {
    setLoading(btn, false);
  }
}

// ── DELIVERY SETTINGS ─────────────────────────────────────
async function saveDeliverySettings() {
  const btn = document.querySelector('[onclick="saveDeliverySettings()"]');
  try {
    setLoading(btn, true);
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({
      delivery_enabled:  document.getElementById('set-delivery-enabled')?.checked,
      delivery_fee:      parseFloat(_getVal('set-delivery-fee') || '0'),
      delivery_time:     _getVal('set-delivery-time'),
      prep_time:         _getVal('set-prep-time'),
      delivery_zones:    _getVal('set-delivery-zones'),
      pickup_notes:      _getVal('set-pickup-notes'),
      delivery_notes:    _getVal('set-delivery-notes'),
    })});
    toast('✅ Delivery settings saved');
  } catch(e) { toast('Failed: ' + e.message, true); }
  finally { setLoading(btn, false); }
}

// ── AI SETTINGS ───────────────────────────────────────────
let _aiTone = 'friendly';
function selectTone(btn) {
  document.querySelectorAll('.tone-pill').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  _aiTone = btn.dataset.tone;
}
async function saveAISettings() {
  const btn = document.querySelector('[onclick="saveAISettings()"]');
  try {
    setLoading(btn, true);
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({
      ai_tone:            _aiTone,
      welcome_message:    _getVal('set-welcome-msg'),
      response_length:    _getVal('set-response-length'),
      recommendations:    document.getElementById('set-recommendations')?.checked,
      upsells:            document.getElementById('set-upsells')?.checked,
      personalised:       document.getElementById('set-personalised')?.checked,
      footer_message:     _getVal('set-footer-msg'),
    })});
    toast('✅ AI settings saved');
  } catch(e) { toast('Failed: ' + e.message, true); }
  finally { setLoading(btn, false); }
}

// ── STORE SETTINGS ────────────────────────────────────────
async function saveStoreControl(key, val) {
  try {
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({ [key]: val }) });
    toast('✅ ' + (val ? 'Enabled' : 'Disabled'));
  } catch(e) { toast('Failed: ' + e.message, true); }
}
async function saveStoreSettings() {
  const btn = document.querySelector('[onclick="saveStoreSettings()"]');
  try {
    setLoading(btn, true);
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({
      offline_message: _getVal('set-offline-msg'),
    })});
    toast('✅ Store settings saved');
  } catch(e) { toast('Failed: ' + e.message, true); }
  finally { setLoading(btn, false); }
}

// ── NOTIFICATION SETTINGS ─────────────────────────────────
async function saveNotifSettings() {
  const btn = document.querySelector('[onclick="saveNotifSettings()"]');
  try {
    setLoading(btn, true);
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({
      email_alerts:   document.getElementById('set-email-alerts')?.checked,
      alert_email:    _getVal('set-alert-email'),
      stock_alerts:   document.getElementById('set-stock-alerts')?.checked,
      daily_summary:  document.getElementById('set-daily-summary')?.checked,
    })});
    toast('✅ Notification settings saved');
  } catch(e) { toast('Failed: ' + e.message, true); }
  finally { setLoading(btn, false); }
}

// saveSettings() is now handled by saveProfile(), saveEcoCashSettings() etc.
// Kept as a no-op for backward compat with any stray calls.
async function saveSettings() { toast('Please use the Settings tabs to save.'); }

// saveBusinessName is now part of saveProfile() above
async function saveBusinessName() { await saveProfile(); }

// ── Account & Security ─────────────────────────────────────────────────────────

async function saveLoginEmail() {
  const email  = (document.getElementById('sec-owner-email')?.value || '').trim();
  const status = document.getElementById('sec-change-status');
  if (!email || !email.includes('@')) { toast('Enter a valid email address', true); return; }
  try {
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({ owner_email: email }) });
    toast('✅ Login email updated');
  } catch(e) { toast('Failed: ' + e.message, true); }
}

function checkNewPassword() {
  const pw   = document.getElementById('sec-pass-new')?.value || '';
  const segs = [1,2,3,4].map(i => document.getElementById('pw-seg' + i));
  const checks = [pw.length >= 8, /[A-Z]/.test(pw), /[0-9]/.test(pw), /[^A-Za-z0-9]/.test(pw)];
  const score  = checks.filter(Boolean).length;
  const colors = ['#ef4444','#f59e0b','#22c55e','#22c55e'];
  segs.forEach((s, i) => { if (s) s.style.background = i < score ? colors[score-1] : 'var(--border)'; });
  // Check confirm match
  const confirm = document.getElementById('sec-pass-confirm')?.value || '';
  const matchEl = document.getElementById('sec-pass-match');
  if (matchEl && confirm) {
    matchEl.textContent = pw === confirm ? '✓ Passwords match' : '✗ Passwords do not match';
    matchEl.style.color = pw === confirm ? 'var(--green)' : '#ef4444';
  }
}

async function changePassword() {
  const current = document.getElementById('sec-pass-current')?.value || '';
  const newPw   = document.getElementById('sec-pass-new')?.value || '';
  const confirm = document.getElementById('sec-pass-confirm')?.value || '';
  const statusEl= document.getElementById('sec-change-status');

  if (!current)              { toast('Enter your current password', true); return; }
  if (newPw.length < 8)      { toast('New password must be at least 8 characters', true); return; }
  if (!/[A-Z]/.test(newPw))  { toast('New password needs an uppercase letter', true); return; }
  if (!/[0-9]/.test(newPw))  { toast('New password needs a number', true); return; }
  if (newPw !== confirm)     { toast('New passwords do not match', true); return; }
  if (newPw === current)     { toast('New password must be different from current', true); return; }

  const btn = document.querySelector('[onclick="changePassword()"]');
  try {
    setLoading(btn, true);
    if (statusEl) statusEl.textContent = '';
    await apiFetch('/auth/change-password', {
      method: 'POST',
      body:   JSON.stringify({ current_password: current, new_password: newPw })
    });
    // Clear fields on success
    ['sec-pass-current','sec-pass-new','sec-pass-confirm'].forEach(id => {
      const el = document.getElementById(id); if (el) el.value = '';
    });
    [1,2,3,4].forEach(i => {
      const s = document.getElementById('pw-seg' + i);
      if (s) s.style.background = 'var(--border)';
    });
    if (statusEl) { statusEl.textContent = '✅ Password updated successfully.'; statusEl.style.color = 'var(--green)'; }
    toast('✅ Password updated');
  } catch(e) {
    if (statusEl) { statusEl.textContent = '❌ ' + e.message; statusEl.style.color = '#ef4444'; }
    toast('Failed: ' + e.message, true);
  }
  finally { setLoading(btn, false); }
}

function setTheme(theme, silent=false) {
  if (theme === 'light') {
    document.body.classList.add('light');
  } else {
    document.body.classList.remove('light');
  }
  localStorage.setItem('wazi_theme', theme);
  const db = document.getElementById('theme-dark-btn');
  const lb = document.getElementById('theme-light-btn');
  if (db) { db.style.color = theme==='dark' ? 'var(--green)' : ''; db.style.borderColor = theme==='dark' ? 'var(--green-dim)' : ''; }
  if (lb) { lb.style.color = theme==='light' ? 'var(--green)' : ''; lb.style.borderColor = theme==='light' ? 'var(--green-dim)' : ''; }
  if (!silent) toast(theme === 'light' ? '☀️ Light mode' : '🌙 Dark mode');
}

function applyFont(font, silent=false) {
  document.body.style.fontFamily = font;
  localStorage.setItem('wazi_font', font);
  // Also persist to Supabase so font survives clearing localStorage / new devices.
  // Fire-and-forget — merges into existing features_json, never wipes other keys.
  if (!silent) {
    apiFetch('/me').then(b => {
      const existing = (b && b.features_json) ? b.features_json : {};
      return apiFetch('/me', { method: 'PATCH', body: JSON.stringify({
        features_json: { ...existing, dashboard_font: font }
      }) });
    }).catch(() => {});
  }
}

// ── UTILS ─────────────────────────────────────────────────
function fmtTime(iso){
  if(!iso) return '—';
  try {
    let s = String(iso).trim();
    // Replace space separator with T (only first occurrence)
    s = s.replace(' ', 'T');
    // Normalise timezone: "+00" → "+00:00", remove microseconds for Safari compat
    s = s.replace(/(\.\d{3})\d+/, '$1');      // trim microseconds to 3dp
    s = s.replace(/([+-]\d{2})$/, '$1:00');   // +00 → +00:00
    if (!/[Z+\-]/.test(s.slice(10))) s += 'Z'; // no tz at all → assume UTC
    const d = new Date(s);
    if(isNaN(d.getTime())) return '—';
    const now = new Date();
    const diff = now - d;
    if(diff < 60000 && diff >= 0) return 'just now';
    return d.toLocaleString('en-GB',{day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'});
  } catch { return '—'; }
}
function escHtml(s){
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

// Non-text WhatsApp messages are stored as a fixed placeholder token
// (see backend/routes/webhook_routes.py) rather than real text — the
// conversation-row preview showed that raw token verbatim ("[image]",
// "[voice_note]", …) instead of a readable label. Maps only the exact
// tokens the backend actually writes; anything else passes through
// unchanged. Mirrors the same map in inbox.js (Live Inbox) so both
// conversation views describe media messages the same way.
const _MEDIA_PREVIEW_LABELS = {
  '[image]':        '📷 Image',
  '[video]':        '🎥 Video',
  '[voice_note]':   '🎤 Voice message',
  '[location]':     '📍 Location',
  '[contact_card]': '👤 Contact',
};
function formatMessagePreview(text) {
  const t = (text === null || text === undefined) ? '' : String(text);
  return _MEDIA_PREVIEW_LABELS[t] || t;
}

// H4: After signup with a pre-selected plan, redirect to Stripe checkout once.
// Fires on first init() after signup from pricing page. Clears immediately so
// it never runs twice. Fails silently — user stays on dashboard if checkout fails.
function checkPendingCheckout() {
  const tier   = localStorage.getItem('wazi_pending_tier');
  const period = localStorage.getItem('wazi_pending_period') || 'monthly';
  if (!tier || !token) return;

  // Clear immediately — only redirect once regardless of outcome
  localStorage.removeItem('wazi_pending_tier');
  localStorage.removeItem('wazi_pending_period');

  // Small delay so dashboard renders first before redirect
  setTimeout(async () => {
    try {
      const res = await apiFetch('/billing/checkout', {
        method: 'POST',
        body: JSON.stringify({ tier: tier, billing_period: period }),
      });
      if (res && res.url) {
        window.location.href = res.url;
      }
    } catch (e) {
      // Non-fatal: user remains on dashboard, can upgrade manually
      console.warn('H4: pending checkout redirect failed:', e);
    }
  }, 1500);
}

// ── User Profile Management ───────────────────────────────────────────────────

let _upData = null;       // cached profile data
let _upUsernameTimer = null;

// ── Load & render ─────────────────────────────────────────────────────────────
async function loadUserProfile() {
  if (_upData) { _upRender(_upData); return; }
  try {
    const data = await apiFetch('/me');
    if (!data) return;
    _upData = data;
    _upRender(data);
  } catch(e) {
    console.warn('[profile] load failed:', e.message);
  }
}

function _upRender(data) {
  const p  = data.user_profile || {};
  const up = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };
  const ut = (id, txt) => { const el = document.getElementById(id); if (el) el.textContent = txt || '—'; };

  // Profile fields
  up('up-owner-name',    p.owner_name   || '');
  up('up-display-name',  p.display_name || '');
  up('up-email',         data.owner_email || '');
  up('up-phone',         p.phone        || '');
  up('up-biz-name',      data.name      || '');
  up('up-bio',           p.bio          || '');

  // Selects
  _upSetSelect('up-country',     p.country   || '');
  _upSetSelect('up-timezone',    p.timezone  || 'Africa/Harare');
  _upSetSelect('up-language',    p.language  || 'en');
  _upSetSelect('up-date-format', p.date_format || 'DD/MM/YYYY');

  // Currency (read-only display from features_json)
  const currEl = document.getElementById('up-currency-display');
  if (currEl) currEl.value = (data.features_json || {}).currency || data.currency_symbol || 'USD';

  // Avatar
  _upSetAvatar(p.avatar_url, p.owner_name || p.display_name || data.owner_username || 'W');

  // Name above avatar
  const nameEl = document.getElementById('up-avatar-name');
  if (nameEl) nameEl.textContent = p.display_name || p.owner_name || data.owner_username || '';

  // Account info (read-only)
  ut('up-info-bid',      data.id);
  ut('up-info-username', '@' + (data.owner_username || ''));
  ut('up-info-created',  data.created_at ? new Date(data.created_at).toLocaleDateString() : '—');
  ut('up-info-plan',     (data.subscription_tier || 'trial').charAt(0).toUpperCase() + (data.subscription_tier || 'trial').slice(1));
  ut('up-info-billing',  data.billing_status || '—');
  ut('up-info-role',     data.is_superadmin ? 'Super Admin' : 'Business Owner');

  // Trial status
  const trialEnds = data.trial_ends_at;
  if (trialEnds) {
    const days = Math.max(0, Math.ceil((new Date(trialEnds) - Date.now()) / 86400000));
    ut('up-info-trial', days > 0 ? `${days} days remaining` : 'Trial ended');
  } else {
    ut('up-info-trial', data.billing_status === 'active' ? 'Subscribed' : '—');
  }

  // Preferences
  const prefs = p.prefs || {};
  const _setChk = (id, val) => { const el = document.getElementById(id); if (el) el.checked = val !== false; };
  _setChk('pref-dark-mode',            prefs.dark_mode);
  _setChk('pref-email-notifications',  prefs.email_notifications);
  _setChk('pref-wa-notifications',     prefs.wa_notifications);
  _setChk('pref-marketing-emails',     prefs.marketing_emails !== undefined ? prefs.marketing_emails : false);
  _setChk('pref-weekly-reports',       prefs.weekly_reports);
  _setChk('pref-system-alerts',        prefs.system_alerts);
}

function _upSetSelect(id, val) {
  const el = document.getElementById(id);
  if (!el || !val) return;
  const opt = Array.from(el.options).find(o => o.value === val);
  if (opt) el.value = val;
}

function _upSetAvatar(url, name) {
  const img      = document.getElementById('up-avatar-img');
  const initials = document.getElementById('up-avatar-initials');
  const removeBtn= document.getElementById('up-avatar-remove-btn');
  if (url && img) {
    img.src = url;
    img.style.display = 'block';
    if (initials) initials.style.display = 'none';
    if (removeBtn) removeBtn.style.display = 'inline-flex';
  } else {
    if (img) img.style.display = 'none';
    if (initials) {
      initials.style.display = '';
      initials.textContent = (name || 'W')[0].toUpperCase();
    }
    if (removeBtn) removeBtn.style.display = 'none';
  }
}

// ── Save profile ──────────────────────────────────────────────────────────────
async function saveUserProfile() {
  const btn    = document.getElementById('up-save-btn');
  const status = document.getElementById('up-profile-status');
  _upSetStatus(status, 'Saving…', 'saving');
  btn.disabled = true;

  const payload = {
    owner_name:   document.getElementById('up-owner-name')?.value.trim()   || null,
    display_name: document.getElementById('up-display-name')?.value.trim() || null,
    user_phone:   document.getElementById('up-phone')?.value.trim()        || null,
    bio:          document.getElementById('up-bio')?.value.trim()          || null,
    country:      document.getElementById('up-country')?.value             || null,
    timezone:     document.getElementById('up-timezone')?.value            || null,
    language:     document.getElementById('up-language')?.value            || null,
    date_format:  document.getElementById('up-date-format')?.value         || null,
  };

  // Also save email via existing saveLoginEmail pattern
  const email = document.getElementById('up-email')?.value.trim();

  try {
    const res = await apiFetch('/me/user-profile', { method: 'PATCH', body: JSON.stringify(payload) });
    if (!res || res.error) throw new Error(res?.error || 'Save failed');

    // Save email separately via PATCH /me
    if (email) {
      await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({ owner_email: email }) });
    }

    _upData = null; // invalidate cache
    _upSetStatus(status, '✓ Profile updated successfully', 'ok');
    toast('Profile updated');
    setTimeout(() => _upSetStatus(status, '', ''), 3000);
  } catch(e) {
    _upSetStatus(status, '✗ ' + (e.message || 'Save failed'), 'err');
  } finally {
    btn.disabled = false;
  }
}

// ── Save preferences ─────────────────────────────────────────────────────────
async function saveUserPreferences() {
  const btn    = document.getElementById('up-prefs-btn');
  const status = document.getElementById('up-prefs-status');
  _upSetStatus(status, 'Saving…', 'saving');
  btn.disabled = true;

  const payload = {
    pref_dark_mode:            document.getElementById('pref-dark-mode')?.checked           ?? true,
    pref_email_notifications:  document.getElementById('pref-email-notifications')?.checked ?? true,
    pref_wa_notifications:     document.getElementById('pref-wa-notifications')?.checked    ?? true,
    pref_marketing_emails:     document.getElementById('pref-marketing-emails')?.checked    ?? false,
    pref_weekly_reports:       document.getElementById('pref-weekly-reports')?.checked      ?? true,
    pref_system_alerts:        document.getElementById('pref-system-alerts')?.checked       ?? true,
  };

  try {
    const res = await apiFetch('/me/user-profile', { method: 'PATCH', body: JSON.stringify(payload) });
    if (!res || res.error) throw new Error(res?.error || 'Save failed');
    _upData = null;
    _upSetStatus(status, '✓ Preferences saved', 'ok');
    toast('Preferences saved');
    setTimeout(() => _upSetStatus(status, '', ''), 3000);
  } catch(e) {
    _upSetStatus(status, '✗ ' + (e.message || 'Save failed'), 'err');
  } finally {
    btn.disabled = false;
  }
}

function upPrefChanged() {
  // Auto-save prefs on toggle change (debounced)
  clearTimeout(window._upPrefTimer);
  window._upPrefTimer = setTimeout(saveUserPreferences, 800);
}

// ── Username change ───────────────────────────────────────────────────────────
let _upUsernameAvailable = false;

async function checkUsernameAvailability(val) {
  clearTimeout(_upUsernameTimer);
  const check = document.getElementById('up-username-check');
  const msg   = document.getElementById('up-username-msg');
  if (!val || val.length < 3) {
    if (check) check.textContent = '';
    if (msg)   msg.textContent = '';
    _upUsernameAvailable = false;
    return;
  }
  if (check) { check.textContent = '…'; check.style.color = 'var(--text-dim)'; }
  _upUsernameTimer = setTimeout(async () => {
    try {
      const res = await apiFetch('/me/check-username', { method: 'POST', body: JSON.stringify({ username: val.trim().toLowerCase() }) });
      if (res.available) {
        check.textContent = '✓ Available';
        check.style.color = 'var(--green)';
        if (msg) { msg.textContent = ''; }
        _upUsernameAvailable = true;
      } else {
        check.textContent = '✗';
        check.style.color = 'var(--red,#ef4444)';
        if (msg) { msg.textContent = res.reason || 'Username already in use'; msg.style.color = 'var(--red,#ef4444)'; }
        _upUsernameAvailable = false;
      }
    } catch(e) { if (check) check.textContent = ''; }
  }, 500);
}

async function saveUsername() {
  const newUser = document.getElementById('up-new-username')?.value.trim().toLowerCase();
  const curPass = document.getElementById('up-username-current-pass')?.value;
  const btn     = document.getElementById('up-username-btn');
  const status  = document.getElementById('up-username-status');

  if (!newUser) { _upSetStatus(status, 'Enter a new username', 'err'); return; }
  if (!_upUsernameAvailable) { _upSetStatus(status, 'Username not available', 'err'); return; }
  if (!curPass) { _upSetStatus(status, 'Enter your current password to confirm', 'err'); return; }

  btn.disabled = true;
  _upSetStatus(status, 'Saving…', 'saving');
  try {
    const res = await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({ owner_username: newUser, current_password: curPass }) });
    if (!res || res.error) throw new Error(res?.detail || res?.error || 'Update failed');
    _upData = null;
    _upSetStatus(status, '✓ Username updated', 'ok');
    toast('Username updated to @' + newUser);
    document.getElementById('up-new-username').value = '';
    document.getElementById('up-username-current-pass').value = '';
    setTimeout(() => _upSetStatus(status, '', ''), 3000);
  } catch(e) {
    _upSetStatus(status, '✗ ' + (e.message || 'Update failed'), 'err');
  } finally {
    btn.disabled = false;
  }
}

// ── Password change ───────────────────────────────────────────────────────────
function upCheckPwStrength(pw) {
  const checks = {
    'up-req-len':   pw.length >= 8,
    'up-req-upper': /[A-Z]/.test(pw),
    'up-req-lower': /[a-z]/.test(pw),
    'up-req-num':   /[0-9]/.test(pw),
  };
  let score = 0;
  Object.entries(checks).forEach(([id, met]) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('met', met);
    if (met) score++;
  });
  const colors = ['','#ef4444','#f59e0b','#f59e0b','#22c55e'];
  const labels = ['','Weak','Fair','Good','Strong'];
  for (let i = 1; i <= 4; i++) {
    const seg = document.getElementById('up-pw-s' + i);
    if (seg) seg.style.background = i <= score ? colors[score] : 'var(--border)';
  }
  const lbl = document.getElementById('up-pw-strength-label');
  if (lbl) { lbl.textContent = pw ? labels[score] : ''; lbl.style.color = colors[score] || 'var(--text-dim)'; }
  upCheckPwMatch();
}

function upCheckPwMatch() {
  const pw  = document.getElementById('up-pass-new')?.value    || '';
  const cpw = document.getElementById('up-pass-confirm')?.value || '';
  const msg = document.getElementById('up-pass-match-msg');
  if (!msg) return;
  if (!cpw) { msg.textContent = ''; return; }
  if (pw === cpw) { msg.textContent = '✓ Passwords match'; msg.style.color = 'var(--green)'; }
  else            { msg.textContent = '✗ Passwords do not match'; msg.style.color = 'var(--red,#ef4444)'; }
}

async function upChangePassword() {
  const cur    = document.getElementById('up-pass-current')?.value;
  const nw     = document.getElementById('up-pass-new')?.value;
  const conf   = document.getElementById('up-pass-confirm')?.value;
  const btn    = document.getElementById('up-pw-btn');
  const status = document.getElementById('up-pw-status');

  if (!cur)             { _upSetStatus(status, 'Enter your current password', 'err'); return; }
  if (!nw || nw.length < 8) { _upSetStatus(status, 'New password must be at least 8 characters', 'err'); return; }
  if (nw !== conf)      { _upSetStatus(status, 'Passwords do not match', 'err'); return; }

  btn.disabled = true;
  _upSetStatus(status, 'Saving…', 'saving');
  try {
    const res = await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({ current_password: cur, owner_password: nw }) });
    if (!res || res.error) throw new Error(res?.detail || res?.error || 'Update failed');
    _upData = null;
    _upSetStatus(status, '✓ Password updated successfully', 'ok');
    toast('Password updated');
    ['up-pass-current','up-pass-new','up-pass-confirm'].forEach(id => {
      const el = document.getElementById(id); if (el) el.value = '';
    });
    setTimeout(() => _upSetStatus(status, '', ''), 4000);
  } catch(e) {
    _upSetStatus(status, '✗ ' + (e.message || 'Update failed'), 'err');
  } finally {
    btn.disabled = false;
  }
}

// ── Avatar ────────────────────────────────────────────────────────────────────
async function uploadAvatar(input) {
  const file = input?.files?.[0];
  if (!file) return;
  const status = document.getElementById('up-profile-status');
  _upSetStatus(status, 'Uploading…', 'saving');

  const form = new FormData();
  form.append('file', file);
  try {
    const token = localStorage.getItem('wazi_token') || '';
    const resp = await fetch('/me/upload-avatar', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token },
      body: form,
    });
    const data = await resp.json();
    if (!resp.ok || !data.avatar_url) throw new Error(data.detail || 'Upload failed');
    _upSetAvatar(data.avatar_url, '');
    if (_upData) { (_upData.user_profile = _upData.user_profile || {}).avatar_url = data.avatar_url; }
    _upSetStatus(status, '✓ Photo updated', 'ok');
    toast('Profile photo updated');
    setTimeout(() => _upSetStatus(status, '', ''), 3000);
  } catch(e) {
    _upSetStatus(status, '✗ ' + (e.message || 'Upload failed'), 'err');
  } finally {
    input.value = '';
  }
}

async function removeAvatar() {
  const status = document.getElementById('up-profile-status');
  _upSetStatus(status, 'Removing…', 'saving');
  try {
    await apiFetch('/me/user-profile', { method: 'PATCH', body: JSON.stringify({ avatar_url: '' }) });
    _upSetAvatar('', (_upData?.user_profile?.owner_name || 'W'));
    if (_upData?.user_profile) _upData.user_profile.avatar_url = '';
    _upSetStatus(status, '✓ Photo removed', 'ok');
    toast('Profile photo removed');
    setTimeout(() => _upSetStatus(status, '', ''), 3000);
  } catch(e) {
    _upSetStatus(status, '✗ ' + e.message, 'err');
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function togglePwVis(inputId, btn) {
  const inp = document.getElementById(inputId);
  if (!inp) return;
  const isHidden = inp.type === 'password';
  inp.type = isHidden ? 'text' : 'password';
  if (btn) btn.textContent = isHidden ? '🙈' : '👁';
}

function _upSetStatus(el, msg, type) {
  if (!el) return;
  el.textContent = msg;
  el.className = 'up-status-' + (type || 'ok');
}

// ── Customer Acquisition Analytics ───────────────────────────────────────────

async function loadAcquisitionStats() {
  try {
    const a = await apiFetch('/analytics/acquisition');
    if (!a) return;

    const _el = (id, val) => { const e = document.getElementById(id); if(e) e.textContent = val; };

    _el('acq-qr-total',    a.qr_scans             ?? '0');
    _el('acq-link-total',  a.whatsapp_clicks       ?? '0');
    _el('acq-conv-total',  a.conversations_started ?? '0');
    _el('acq-orders',      a.orders                ?? '0');
    _el('acq-conversion',  (a.conversion_rate ?? 0) + '%');
    _el('acq-qr-today',    a.today?.qr_scans       ?? '0');
    _el('acq-link-today',  a.today?.whatsapp_clicks ?? '0');
    _el('acq-conv-today',  a.today?.conversations_started ?? '0');

    // Funnel bars — relative to QR scans as 100%
    const max = Math.max(a.qr_scans || 1, 1);
    const _bar = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.style.width = Math.min(100, Math.round(val / max * 100)) + '%';
    };
    _bar('acq-bar-qr',    a.qr_scans             || 0);
    _bar('acq-bar-conv',  a.conversations_started || 0);
    _bar('acq-bar-orders', a.orders              || 0);

  } catch(e) {
    console.warn('Acquisition stats load failed:', e.message);
  }
}

// ── Marketing Kit ─────────────────────────────────────────────────────────────

let _mktData = null;

async function loadMarketingKit() {
  if (_mktData) { renderMarketingKit(_mktData); return; }
  try {
    _mktData = await apiFetch('/marketing/kit');
    renderMarketingKit(_mktData);
    loadMarketingQR();
  } catch(e) { console.warn('Marketing kit load failed:', e.message); }
}

function renderMarketingKit(data) {
  if (!data || data.error) return;
  const _el = (id, val) => { const e = document.getElementById(id); if(e) e.textContent = val; };
  _el('mkt-wa-link', data.whatsapp_link || '—');
  _el('mkt-keyword',  data.keyword       || '—');
  _el('mkt-shared-number', `Send to WaziBot: ${data.shared_number || ''}`);
  // Wire download buttons to authenticated blob download
  ['mkt-dl-table','mkt-dl-flyer','mkt-dl-receipt','mkt-qr-download'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.href = '#'; el.onclick = (e) => { e.preventDefault(); mktDownloadQR(); }; }
  });
}

let _qrDest = 'whatsapp';

function setQrDest(dest) {
  _qrDest = dest;
  const wa = document.getElementById('qr-dest-whatsapp');
  const bk = document.getElementById('qr-dest-booking');
  if (wa) wa.className = dest === 'whatsapp' ? 'btn btn-primary' : 'btn btn-ghost';
  if (bk) bk.className = dest === 'booking'  ? 'btn btn-primary' : 'btn btn-ghost';
  wa && (wa.style.cssText = 'padding:6px 14px;font-size:12px;');
  bk && (bk.style.cssText = 'padding:6px 14px;font-size:12px;');
  mktRefreshQR();
}

async function loadMarketingQR() {
  const img = document.getElementById('mkt-qr-img');
  const loading = document.getElementById('mkt-qr-loading');
  const errEl   = document.getElementById('mkt-qr-error');
  const dlBtn   = document.getElementById('mkt-qr-download');
  if (!img) return;
  try {
    const token = localStorage.getItem('wazi_token') || '';
    const resp  = await fetch('/marketing/qr?dest=' + encodeURIComponent(_qrDest), { headers: { 'Authorization': `Bearer ${token}` } });
    if (!resp.ok) throw new Error(await resp.text());
    const blob = await resp.blob();
    img.src = URL.createObjectURL(blob);
    img.style.display   = 'block';
    if (loading) loading.style.display = 'none';
    if (errEl)   errEl.style.display   = 'none';
    if (dlBtn)   dlBtn.style.display   = 'inline-flex';
  } catch(e) {
    if (loading) loading.style.display = 'none';
    if (errEl) { errEl.textContent = 'Could not generate QR: ' + e.message; errEl.style.display = 'block'; }
  }
}

async function mktDownloadQR() {
  try {
    const token = localStorage.getItem('wazi_token') || '';
    const resp  = await fetch('/marketing/qr/download?dest=' + encodeURIComponent(_qrDest), { headers: { 'Authorization': `Bearer ${token}` } });
    if (!resp.ok) throw new Error('Download failed');
    const blob  = await resp.blob();
    const url   = URL.createObjectURL(blob);
    const fname = (_mktData?.slug || 'business') + '-' + _qrDest + '-qr.png';
    const a = document.createElement('a');
    a.href = url; a.download = fname;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast('QR downloaded: ' + fname);
  } catch(e) { toast('Download failed: ' + e.message, true); }
}

function mktRefreshQR() {
  _mktData = null;
  const img = document.getElementById('mkt-qr-img');
  const loading = document.getElementById('mkt-qr-loading');
  if (img) { img.src = ''; img.style.display = 'none'; }
  if (loading) loading.style.display = 'block';
  loadMarketingKit();
}

function mktCopy(elementId) {
  const el = document.getElementById(elementId);
  const text = (el?.textContent || el?.value || '').trim();
  if (!text || text === '—') { toast('Nothing to copy', true); return; }
  navigator.clipboard.writeText(text)
    .then(() => toast('Copied!'))
    .catch(() => {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); document.body.removeChild(ta);
      toast('Copied!');
    });
}

function mktOpen() {
  if (_mktData?.whatsapp_link) window.open(_mktData.whatsapp_link, '_blank');
  else toast('Link not loaded yet', true);
}

// ── INIT ──────────────────────────────────────────────────
async function init(){
  // Show help FAB now that user is logged in
  try { injectDashboardHelp(); } catch(_) {}
  const savedTheme = localStorage.getItem('wazi_theme') || 'dark';
  const savedFont = localStorage.getItem('wazi_font');
  setTheme(savedTheme, true);
  if (savedFont) document.body.style.fontFamily = savedFont;

  buildSidebar();
  checkStatus();

  // Handle Stripe Connect return from onboarding
  const _urlParams = new URLSearchParams(window.location.search);
  if (_urlParams.get('stripe_connect') === 'success') {
    toast('✅ Stripe account connected! Loading your payment status…');
    // Remove param from URL without reload
    window.history.replaceState({}, '', window.location.pathname);
    // Reload connect status after short delay (Stripe may need a moment to propagate)
    setTimeout(loadStripeConnectStatus, 2000);
  } else if (_urlParams.get('stripe_connect') === 'refresh') {
    toast('↩ Stripe setup incomplete — you can reconnect anytime from Settings → Payments.', true);
    window.history.replaceState({}, '', window.location.pathname);
  }

  // Deep link from Live Inbox's "Orders" quick action (?openOrdersFor=<phone>)
  // — jumps straight to that customer's orders instead of the dashboard home.
  const _openOrdersFor = _urlParams.get('openOrdersFor');
  if (_openOrdersFor) {
    window.history.replaceState({}, '', window.location.pathname);
    setTimeout(() => {
      showSection('orders', null);
      const search = document.getElementById('order-search');
      if (search) { search.value = _openOrdersFor; filterOrdersBySearch(_openOrdersFor); }
    }, 500);
  }

  // Deep link from Live Inbox's "Customer" quick action (?openCrmFor=<phone>)
  // — Phase 7: Live Inbox previously had no way to jump to the full CRM
  // record at all, only to Orders. Jumps to Customers and opens that
  // person's existing drawer, same lookup pattern as viewCustomerFromChat().
  const _openCrmFor = _urlParams.get('openCrmFor');
  if (_openCrmFor) {
    window.history.replaceState({}, '', window.location.pathname);
    setTimeout(() => {
      showSection('crm', null);
      setTimeout(() => {
        const match = (_crmTableData || []).find(c => c.phone === _openCrmFor);
        if (match) { openCustomerDrawer(match); }
        else { toast('No CRM record for ' + _openCrmFor + ' yet', true); }
      }, 400);
    }, 500);
  }

  // Load currency symbol BEFORE any money rendering, so the first paint of
  // Orders/Products/stats is correct rather than briefly flashing '$' and
  // never refreshing (this was the root cause of currency "not applying
  // across the system" — it simply wasn't fetched early/at all).
  if (token) { try { await getCachedMe(); } catch (_) {} }

  if(userRole==='superadmin'){loadAdminData();}
  else{loadOrders();loadProducts();loadConversations();loadCustomerStats();try{loadNeedsAttention();}catch(_){}}
  // H4: redirect to Stripe checkout if user just signed up from pricing page
  checkPendingCheckout();
  // Sprint 8: single consolidated post-auth init (replaces scattered DOM listeners)
  _postLoginInit();
}

// Sprint 8: All post-auth initialisation in one place.
// Every function here runs ONCE per page load, with staggered delays to avoid
// hammering Supabase simultaneously. Replaces the scattered window.addEventListener
// and appended DOMContentLoaded blocks from previous sprint sessions.
function _postLoginInit() {
  if (!token) return;  // not logged in — nothing to do

  // Immediate: trial/restriction status — this was previously defined but
  // never actually called anywhere, so the trial-expired banner and any
  // feature restriction never appeared regardless of billing status. Runs
  // right away (not delayed) since sidebar clicks can happen immediately.
  try { checkUpgradePrompts(); } catch(_) {}

  // 1.5 s: repeat customer stat (lightweight analytics query)
  setTimeout(() => {
    try { loadRepeatCustomerStat(); }   catch(_) {}
    try { loadSatisfactionScore();  }   catch(_) {}
  }, 1500);

  // 2 s: heavier UI features that depend on full session being established
  setTimeout(() => {
    try { checkFirstOrderCelebration(); } catch(_) {}
    try { loadHealthWidget();           } catch(_) {}
    try { showShareStoreBanner();       } catch(_) {}
  }, 2000);
}

// If access token is valid → show dashboard immediately
// If access token expired but refresh token exists → silently refresh, then init
// If neither → show login screen
if (token && userRole) {
  const _ls4 = document.getElementById('login-screen');
  if (_ls4) _ls4.style.display = 'none';
    const _sbX = document.getElementById('sidebar');
    const _sbtX = document.getElementById('sidebar-toggle-btn');
    if (_sbX)  _sbX.style.display  = '';
    if (_sbtX) _sbtX.style.display = '';
  init();
} else if (!token && refreshTok && userRole) {
  // Access token expired on load — try silent refresh before showing login
  (async () => {
    const ok = await tryRefresh();
    if (ok) {
      const _ls5 = document.getElementById('login-screen');
      if (_ls5) _ls5.style.display = 'none';
    const _sbX = document.getElementById('sidebar');
    const _sbtX = document.getElementById('sidebar-toggle-btn');
    if (_sbX)  _sbX.style.display  = '';
    if (_sbtX) _sbtX.style.display = '';
      init();
    }
    // If refresh fails, login screen stays visible (default state)
  })();
}

function copy(text) {
  navigator.clipboard.writeText(text);
  toast("Copied to clipboard ✅");
}

// 🔄 AUTO REFRESH EVERY 15s
setInterval(() => {
  if (!token) return;
  if (userRole === 'superadmin') {
    loadAdminData().catch(()=>{});
  } else {
    loadOrders().catch(()=>{});
  }
  checkStatus().catch(()=>{});

  // UI/UX bugfix: the Conversations tab previously had NO live-update
  // mechanism at all — a new WhatsApp message would only appear after a
  // manual tab switch or full page reload. Reusing this existing 30s
  // cadence (rather than adding a new, faster interval, which the spec
  // this fix follows explicitly warns against) keeps the list's ordering
  // and unread counts fresh, and refreshes the open chat's messages too,
  // without doing anything on tabs the agent isn't even looking at.
  const convSection = document.getElementById('section-conversations');
  if (convSection && convSection.classList.contains('active')) {
    loadConversations().catch(() => {});
    const win = document.getElementById('chat-window');
    const openPhone = win && win.dataset.activePhone;
    if (openPhone) loadChatMessages(openPhone).catch(() => {});
  }
}, 30000);  // 30s — reduced from 15s to cut Supabase request volume

function setLoading(el, state=true) {
  if (!el) return;
  el.style.opacity = state ? "0.5" : "1";
  el.style.pointerEvents = state ? "none" : "auto";
}

// ════════════════════════════════════════════════════════════════════════════
// PHASE 1 — CRM SEGMENT CARD (overview) + REMINDERS BADGE
// ════════════════════════════════════════════════════════════════════════════

let _overviewExtrasLoading = false;
async function loadOverviewExtras() {
  if (!token) return;
  if (_overviewExtrasLoading) return;
  _overviewExtrasLoading = true;
  // CRM segments
  try {
    const seg = await apiFetch(ROUTES.crmSegments);
    if (seg) {
      ['vip','loyal','regular','new'].forEach(s => {
        const el = document.getElementById('seg-' + s);
        if (el) el.textContent = seg[s] ?? '0';
      });
      const tot = document.getElementById('seg-total');
      if (tot) tot.textContent = seg.total ?? '—';
    }
  } catch (_) {}

  // Payment reminders badge
  try {
    const rem = await apiFetch(ROUTES.reminders);
    const orders = rem && rem.orders ? rem.orders : [];
    const count  = orders.length;
    ['rem-tier1','rem-tier2','rem-tier3'].forEach(id => {
      const el = document.getElementById(id); if (el) el.textContent = '—';
    });
    let t1=0,t2=0,t3=0;
    orders.forEach(o => {
      if (o.reminder_tier===3) t3++;
      else if (o.reminder_tier===2) t2++;
      else t1++;
    });
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    set('rem-tier1', t1); set('rem-tier2', t2); set('rem-tier3', t3);
    set('rem-total', count);
    // Nav badge
    const nb = document.getElementById('nav-rem-badge');
    if (nb) { nb.textContent = count; nb.style.display = count > 0 ? 'inline-flex' : 'none'; }
  } catch (_) {
  } finally {
    _overviewExtrasLoading = false;
  }
}

// Hook into the overview load — IIFE closure avoids const/function TDZ conflict
loadOrders = (function(_wrapped) {
  return async function loadOrders() {
    await _wrapped();
    loadOverviewExtras();
  };
}(loadOrders));

async function sendAllReminders() {
  const btn = event && event.target;
  if (btn) btn.disabled = true;
  try {
    const r = await apiFetch(ROUTES.remindersSend + '?dry_run=false', { method: 'POST' });
    toast(`📨 Sent ${r.sent || 0} reminders (${r.failed || 0} failed)`);
    loadReminders();
    loadOverviewExtras();
  } catch (e) {
    toast('Send failed: ' + e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}


// ════════════════════════════════════════════════════════════════════════════
// PHASE 2 — CAMPAIGN BUILDER
// ════════════════════════════════════════════════════════════════════════════

let _campaignAudiences = {};

async function loadCampaignAudiences() {
  try {
    const auds = await apiFetch(ROUTES.campaignAuds);
    _campaignAudiences = auds || {};
    const lbl = document.getElementById('preview-audience-label');
    if (lbl) updateAudienceLabel();
  } catch (_) {}
}

function onAudienceChange() {
  updateAudienceLabel();
  updatePreview();
  loadCampaignTemplateSuggestions();
}

function updateAudienceLabel() {
  const sel  = document.getElementById('campaign-audience');
  const lbl  = document.getElementById('preview-audience-label');
  if (!sel || !lbl) return;
  const aud  = _campaignAudiences[sel.value];
  lbl.textContent = aud ? aud.desc : '';
}

async function loadCampaignTemplateSuggestions() {
  const container = document.getElementById('campaign-templates');
  if (!container) return;
  // Show audience-relevant template suggestions if available
  const audience = (document.getElementById('campaign-audience') || {}).value || 'all';
  const suggestions = {
    inactive_30d: "Hi {name}, we miss you at {business}! Come back today 🙏 Type menu to order.",
    inactive_14d: "Hi {name}! It's been a while at {business}. New items just arrived — type menu to browse.",
    vip:          "Hey {name}! ⭐ You've placed {orders} orders with us — you're amazing! Special thanks from {business} 🙏",
    new:          "Hi {name}! Thank you for your first order at {business} 🎉 We hope you loved it. Type menu to order again!",
    high_spenders:"Hey {name}! 💰 You're one of our best customers. {business} has something special for you — type menu!",
  };
  if (suggestions[audience]) {
    container.innerHTML = `<div style="background:rgba(34,197,94,0.06);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-family:var(--mono);font-size:11px;color:var(--text-dim);margin-bottom:10px;cursor:pointer;" onclick="document.getElementById('broadcast-msg').value=this.dataset.msg;updatePreview();" data-msg="${escHtml(suggestions[audience])}">
      💡 Suggested: <em style="color:var(--text);">${escHtml(suggestions[audience].slice(0,80))}...</em>
    </div>`;
  } else {
    container.innerHTML = '';
  }
}

async function previewCampaign() {
  const msg = (document.getElementById('broadcast-msg') || {}).value || '';
  const audience = (document.getElementById('campaign-audience') || {}).value || 'all';
  if (!msg.trim()) { toast('Write a message first', true); return; }
  try {
    const r = await apiFetch(ROUTES.campaignPrev, {
      method: 'POST',
      body: JSON.stringify({ audience, message: msg, dry_run: true })
    });
    const samples = document.getElementById('preview-samples');
    const list    = document.getElementById('preview-samples-list');
    if (samples && list && r && r.previews) {
      list.innerHTML = r.previews.map(p =>
        `<div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:6px;font-family:var(--mono);font-size:11px;">
          <div style="color:var(--text-dim);margin-bottom:4px;">📱 ${escHtml(p.phone)}</div>
          <div style="white-space:pre-wrap;">${escHtml(p.message)}</div>
        </div>`
      ).join('');
      samples.style.display = 'block';
      const statSel = document.getElementById('stat-selected');
      if (statSel) statSel.textContent = r.total + ' recipients';
    }
  } catch (e) { toast('Preview failed: ' + e.message, true); }
}

async function sendCampaign() {
  const msg      = (document.getElementById('broadcast-msg') || {}).value || '';
  const audience = (document.getElementById('campaign-audience') || {}).value || 'all';
  const result   = document.getElementById('broadcast-result');
  if (!msg.trim()) { toast('Write a message first', true); return; }
  if (!confirm(`Send to "${audience}" audience?`)) return;
  const btn = document.getElementById('broadcast-send-btn');
  if (btn) btn.disabled = true;
  if (result) result.style.display = 'none';
  try {
    const r = await apiFetch(ROUTES.campaigns, {
      method: 'POST',
      body: JSON.stringify({ audience, message: msg, dry_run: false })
    });
    if (result) {
      result.style.display = 'block';
      result.className = 'broadcast-result ' + (r.failed === 0 ? 'success' : 'error');
      result.innerHTML = `✅ Sent <strong>${r.sent}</strong> | Failed <strong>${r.failed}</strong> | Total <strong>${r.total}</strong>`;
    }
    toast(`📢 Campaign sent to ${r.sent} customers!`);
    if (document.getElementById('stat-last')) document.getElementById('stat-last').textContent = 'Just now';
  } catch (e) {
    if (result) { result.style.display='block'; result.className='broadcast-result error'; result.textContent='❌ '+e.message; }
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}


// ════════════════════════════════════════════════════════════════════════════
// PHASE 3 — CRM SECTION
// ════════════════════════════════════════════════════════════════════════════

async function loadCrm() {
  // Load segment counts for the 4 cards
  try {
    const seg = await apiFetch(ROUTES.crmSegments);
    if (seg) {
      ['vip','loyal','new'].forEach(s => {
        const el = document.getElementById('crm-count-' + s);
        if (el) el.textContent = seg[s] ?? '0';
      });
    }
  } catch (_) {
    // UI/UX audit: these 4 cards used to fail completely silently — no
    // toast, no dash, nothing — leaving them stuck on whatever they last
    // showed with no sign anything was wrong.
    ['vip','loyal','new'].forEach(s => { const el = document.getElementById('crm-count-' + s); if (el) el.textContent = '—'; });
  }

  // Load inactive count (30d) for the 4th card
  try {
    const inactive = await apiFetch(ROUTES.crmInactive + '?days=30');
    const el = document.getElementById('crm-count-inactive');
    if (el) el.textContent = Array.isArray(inactive) ? inactive.length : '—';
  } catch (_) {
    const el = document.getElementById('crm-count-inactive');
    if (el) el.textContent = '—';
  }

  // Load simple customer list
  const tbody = document.getElementById('crm-table-body');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="6"><div class="empty">Loading…</div></td></tr>';
  try {
    const rows = await apiFetch(ROUTES.crmSegments + '/all');
    const data = Array.isArray(rows) ? rows : [];
    _crmTableData = data;   // full, unfiltered — the source of truth for filters below
    // Re-apply whatever search/segment filter was active before this reload
    // (e.g. a background refresh) rather than silently dropping it.
    _renderCrmTable(_applyCrmFilters());
  } catch (e) {
    if (tbody) tbody.innerHTML = _errorRow(6, 'customers', 'loadCrm');
  }
}

// kept for compatibility (called by overview card click)
async function loadCrmSegment(segment) {
  showSection('crm', null);
  loadCrm();
}

// ════════════════════════════════════════════════════════════════════════════
// UI/UX audit: the 4 segment stat cards (VIP/Loyal/New/Haven't ordered) were
// static numbers with no way to actually see who was in a segment, and the
// customer table had no search at all. Both are additive — _crmTableData
// (the full unfiltered list) stays the source of truth; _crmVisibleRows is
// only what's currently rendered, so the "View" button's index always
// matches what's on screen even after filtering.
// ════════════════════════════════════════════════════════════════════════════
let _crmVisibleRows   = [];
let _crmSearchQuery   = '';
let _crmSegmentFilter = 'all';
// UI/UX audit — Phase 9 recommendation ("Broadcast -> reached customers"),
// originally flagged as needing a new CRM multi-phone filter and left for a
// follow-up. null means no phone-set filter active; otherwise a Set of
// phone numbers to restrict the table to (e.g. everyone a broadcast reached).
let _crmPhoneSetFilter = null;
let _crmPhoneSetLabel  = '';

function _applyCrmFilters() {
  let rows = _crmTableData;
  if (_crmPhoneSetFilter) {
    rows = rows.filter(c => _crmPhoneSetFilter.has(c.phone));
  }
  if (_crmSegmentFilter !== 'all') {
    rows = rows.filter(c => {
      if (_crmSegmentFilter === 'inactive') return (c.order_count || 0) === 0 || !c.last_seen;
      return getSegmentLabel(c.order_count || 0, c.total_spent || 0).label.toLowerCase().includes(_crmSegmentFilter);
    });
  }
  if (_crmSearchQuery) {
    const q = _crmSearchQuery;
    rows = rows.filter(c => (c.phone||'').toLowerCase().includes(q) || (c.customer_name||'').toLowerCase().includes(q));
  }
  return rows;
}

// Debounced — only ever triggered by the search box's own oninput/clear,
// same reasoning as filterConversations above.
const filterCrmTable = _debounce(function(query) {
  _crmSearchQuery = (query || '').trim().toLowerCase();
  _renderCrmTable(_applyCrmFilters());
}, 150);

const _CRM_SEGMENT_LABELS = { vip: '⭐ VIP', loyal: '💚 Loyal', new: '👋 New', inactive: "😴 Haven't ordered" };
function filterCrmBySegment(segment) {
  _crmSegmentFilter = segment;
  ['vip','loyal','new','inactive'].forEach(s => {
    const card = document.getElementById('crm-seg-card-' + s);
    if (card) card.style.outline = (s === segment) ? '2px solid var(--green)' : 'none';
  });
  // Picking a segment clears any active phone-set filter (e.g. from a
  // broadcast) — the two are mutually exclusive views of the table.
  _crmPhoneSetFilter = null;
  _crmPhoneSetLabel  = '';
  _updateCrmFilterChip(segment !== 'all' ? _CRM_SEGMENT_LABELS[segment] : null);
  _renderCrmTable(_applyCrmFilters());
}

// Restricts the CRM table to a specific set of phone numbers — e.g. "View
// these N customers" after a broadcast send. Clears any active segment
// filter for the same reason filterCrmBySegment() clears the phone set.
function filterCrmByPhoneSet(phones, label) {
  _crmPhoneSetFilter = new Set(phones);
  _crmPhoneSetLabel  = label || `${phones.length} customer${phones.length!==1?'s':''}`;
  _crmSegmentFilter  = 'all';
  ['vip','loyal','new','inactive'].forEach(s => {
    const card = document.getElementById('crm-seg-card-' + s);
    if (card) card.style.outline = 'none';
  });
  _updateCrmFilterChip(_crmPhoneSetLabel);
  showSection('crm', null);
  _renderCrmTable(_applyCrmFilters());
}

function clearCrmPhoneSetFilter() {
  _crmPhoneSetFilter = null;
  _crmPhoneSetLabel  = '';
  _updateCrmFilterChip(null);
  _renderCrmTable(_applyCrmFilters());
}

function _updateCrmFilterChip(text) {
  const chip = document.getElementById('crm-active-filter-chip');
  if (!chip) return;
  if (!text) { chip.style.display = 'none'; return; }
  chip.style.display = '';
  chip.textContent = `${text} ✕`;
  chip.onclick = _crmPhoneSetFilter ? clearCrmPhoneSetFilter : (() => filterCrmBySegment('all'));
}

function _renderCrmTable(rows) {
  _crmVisibleRows = rows;
  const tbody = document.getElementById('crm-table-body');
  if (!tbody) return;
  if (!rows.length) {
    const msg = (_crmSearchQuery || _crmSegmentFilter !== 'all')
      ? 'No customers match this filter.'
      : "No customers yet.<br>Customers who message you on WhatsApp will appear here.";
    tbody.innerHTML = `<tr><td colspan="6"><div class="empty">${msg}</div></td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((c, i) => {
    return `<tr>
      <td style="font-family:var(--mono);font-size:12px;">${escHtml(c.phone || '—')}</td>
      <td>${escHtml(c.customer_name || '—')}</td>
      <td>${c.order_count || 0}</td>
      <td style="color:var(--green);">${getCurrencySymbol()}${parseFloat(c.total_spent || 0).toFixed(2)}</td>
      <td style="font-family:var(--mono);font-size:11px;color:var(--text-dim);">${c.last_seen ? fmtTime(c.last_seen) : '—'}</td>
      <td><button class="btn btn-ghost" style="font-size:11px;padding:3px 8px;" onclick="openCustomerDrawer(_crmVisibleRows[${i}])">View</button></td>
    </tr>`;
  }).join('');
}

function getSegmentLabel(orders, spent) {
  orders = parseInt(orders) || 0; spent = parseFloat(spent) || 0;
  if (orders >= 10 || spent >= 50) return { label: '⭐ VIP',     cls: 'badge-amber' };
  if (orders >= 5  || spent >= 20) return { label: '💚 Loyal',   cls: 'badge-green' };
  if (orders >= 2)                  return { label: '👍 Regular', cls: 'badge-green' };
  if (orders >= 1)                  return { label: '👋 New',     cls: 'badge-blue'  };
  return                                   { label: '🔍 Prospect',cls: ''            };
}

async function loadInactive(days) {
  const list = document.getElementById('crm-inactive-list');
  if (!list) return;
  list.innerHTML = '<div style="color:var(--text-dim);font-size:11px;">Loading…</div>';
  try {
    const rows = await apiFetch(ROUTES.crmInactive + '?days=' + days);
    const data = Array.isArray(rows) ? rows : [];
    if (!data.length) { list.innerHTML = '<div style="color:var(--text-dim);font-size:11px;padding:8px 0;">No inactive customers 🎉</div>'; return; }
    list.innerHTML = data.slice(0,8).map(c =>
      `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border);font-size:11px;">
        <span>${escHtml(c.phone||'—')}</span>
        <span style="color:var(--text-dim);">${c.order_count||0} orders</span>
      </div>`
    ).join('') + (data.length > 8 ? `<div style="color:var(--text-dim);font-size:10px;padding-top:6px;">+${data.length-8} more</div>` : '');
  } catch (e) {
    list.innerHTML = `<div style="color:var(--red);font-size:11px;">⚠ ${e.message}</div>`;
  }
}

async function campaignInactive() {
  const days = document.getElementById('crm-inactive-days') ? document.getElementById('crm-inactive-days').value : '30';
  const msg  = prompt(`Message to send to inactive customers (${days} days):\nTip: use {name} and {business}`,
    `Hi {name}! We miss you at {business}. Come back today — type menu to order! 😊`);
  if (!msg) return;
  try {
    const r = await apiFetch(ROUTES.campaigns, {
      method: 'POST',
      body: JSON.stringify({ audience: 'inactive_' + days + 'd', message: msg })
    });
    toast(`📢 Sent to ${r.sent} inactive customers`);
  } catch (e) { toast(e.message, true); }
}


// ════════════════════════════════════════════════════════════════════════════
// PHASE 4 — CUSTOMER PROFILE DRAWER
// ════════════════════════════════════════════════════════════════════════════

let _drawerCustomer = null;

function openCustomerDrawer(customer) {
  _drawerCustomer = customer;
  const phone = customer.phone || '—';
  const seg   = getSegmentLabel(customer.order_count, customer.total_spent);
  document.getElementById('drawer-phone').textContent    = phone;
  document.getElementById('drawer-segment').textContent  = seg.label;
  document.getElementById('drawer-orders').textContent   = customer.order_count || 0;
  document.getElementById('drawer-spent').textContent    = getCurrencySymbol() + parseFloat(customer.total_spent||0).toFixed(2);
  document.getElementById('drawer-last').textContent     = customer.last_seen ? fmtTime(customer.last_seen) : '—';
  const nameInput = document.getElementById('drawer-name-input');
  if (nameInput) nameInput.value = customer.customer_name || '';
  document.getElementById('drawer-orders-list').innerHTML = '<div style="color:var(--text-dim);font-family:var(--mono);font-size:11px;">Loading orders…</div>';
  const convEl = document.getElementById('drawer-conversation-list');
  if (convEl) convEl.innerHTML = '<div style="color:var(--text-dim);font-family:var(--mono);font-size:11px;">Loading…</div>';
  const handoffBadge = document.getElementById('drawer-handoff-badge');
  if (handoffBadge) handoffBadge.style.display = 'none';
  document.getElementById('customer-drawer').classList.add('open');
  document.getElementById('drawer-overlay').classList.add('open');
  _focusIntoDrawer('customer-drawer');
  loadDrawerOrders(phone);
  loadDrawerConversation(phone);
  loadDrawerHandoffStatus(phone);
}

// UI/UX audit (Phase 9): reuses the same /analytics/handoff-stats call
// loadHandoffStats() already makes for the Handoff queue — no new endpoint —
// just checked here to flag it on the CRM drawer instead. Fails silently:
// this is a nice-to-have signal, not core drawer content.
async function loadDrawerHandoffStatus(phone) {
  const badge = document.getElementById('drawer-handoff-badge');
  if (!badge) return;
  try {
    const data = await apiFetch('/analytics/handoff-stats');
    if (!_drawerCustomer || _drawerCustomer.phone !== phone) return; // drawer moved on
    const pending = (data && data.pending_handoffs) || [];
    const isPending = pending.some(p => p.phone === phone);
    badge.style.display = isPending ? '' : 'none';
  } catch (_) { /* non-critical */ }
}

// UI/UX audit: reuses the same /chat/conversations/{phone} endpoint that
// powers Live Inbox — read-only preview, last 5 messages, oldest at top.
async function loadDrawerConversation(phone) {
  const list = document.getElementById('drawer-conversation-list');
  if (!list) return;
  try {
    const raw = await apiFetch(`${ROUTES.conversations}/${encodeURIComponent(phone)}`);
    const msgs = (Array.isArray(raw) ? raw : (Array.isArray(raw?.messages) ? raw.messages : [])).slice(-5);
    if (!msgs.length) { list.innerHTML = '<div style="font-family:var(--mono);font-size:11px;color:var(--text-dim);">No messages yet.</div>'; return; }
    list.innerHTML = msgs.map(m => {
      const text  = m.message || m.text || '';
      const isOut = (m.direction||'') === 'outgoing' || (m.direction||'') === 'out';
      return `<div style="padding:6px 0;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:11px;">
        <span style="color:${isOut?'var(--green)':'var(--text-dim)'};">${isOut?'You':'Them'}:</span>
        <span style="color:var(--text);">${escHtml(text.length > 80 ? text.slice(0,80)+'…' : text)}</span>
        <div style="color:var(--text-dim);font-size:10px;margin-top:2px;">${fmtTime(m.created_at || m.createdAt || m.timestamp)}</div>
      </div>`;
    }).join('');
  } catch (e) {
    list.innerHTML = `<div style="font-family:var(--mono);font-size:11px;color:var(--text-dim);">No conversation yet.</div>`;
  }
}

async function saveDrawerName() {
  if (!_drawerCustomer || !_drawerCustomer.phone) return;
  const input = document.getElementById('drawer-name-input');
  const name  = input ? input.value.trim() : '';
  if (!name) { toast('Enter a name first', true); return; }

  try {
    const res = await apiFetch(`/crm/customers/${encodeURIComponent(_drawerCustomer.phone)}/name`, {
      method: 'PATCH',
      body: JSON.stringify({ customer_name: name }),
    });
    if (res && res.ok) {
      _drawerCustomer.customer_name = name;
      toast('✅ Name saved');
      // Refresh whichever lists could show this customer's name
      loadCrm();
      const picker = document.getElementById('bc-customer-picker');
      if (picker && picker.style.display !== 'none') loadBcCustomerPicker();
    } else {
      toast('Could not save name', true);
    }
  } catch (e) {
    toast(e.message || 'Could not save name', true);
  }
}

async function loadDrawerOrders(phone) {
  const list = document.getElementById('drawer-orders-list');
  try {
    const all = await apiFetch(ROUTES.orders);
    const orders = (Array.isArray(all) ? all : (all && all.data ? all.data : []))
      .filter(o => o.customer_phone === phone)
      .slice(0, 5);
    if (!orders.length) { list.innerHTML = '<div style="font-family:var(--mono);font-size:11px;color:var(--text-dim);">No orders yet.</div>'; return; }
    list.innerHTML = orders.map(o =>
      `<div style="display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:11px;">
        <span><span class="badge badge-amber" style="font-size:10px;">#${o.id}</span></span>
        <span style="color:var(--text-dim);">${escHtml(o.product_name||'—')}</span>
        <span style="color:var(--green);">${getCurrencySymbol()}${parseFloat(o.total_price||0).toFixed(2)}</span>
        <span style="color:var(--text-dim);">${fmtTime(o.created_at)}</span>
      </div>`
    ).join('');
  } catch (e) {
    list.innerHTML = `<div style="font-family:var(--mono);font-size:11px;color:var(--red);">⚠ ${e.message}</div>`;
  }
}

function closeDrawer() {
  document.getElementById('customer-drawer').classList.remove('open');
  document.getElementById('drawer-overlay').classList.remove('open');
  _drawerCustomer = null;
  _restoreFocusFromDrawer();
}

// UI/UX audit: Customer → Orders had no working link at all — this jumps
// to the Orders section and reuses the search box added there to filter
// down to just this customer's orders, rather than building a second view.
function viewOrdersForDrawer() {
  if (!_drawerCustomer || !_drawerCustomer.phone) return;
  const phone = _drawerCustomer.phone;
  closeDrawer();
  showSection('orders', null);
  setTimeout(() => {
    const search = document.getElementById('order-search');
    if (search) { search.value = phone; filterOrdersBySearch(phone); }
  }, 300);
}

function openInboxForDrawer() {
  if (_drawerCustomer && _drawerCustomer.phone) {
    // Deep-link straight to this customer's conversation (inbox.js reads
    // ?phone= on load) instead of dropping the agent on the inbox home screen.
    window.open('/inbox?phone=' + encodeURIComponent(_drawerCustomer.phone), '_blank');
  }
  closeDrawer();
}

async function quickCampaignDrawer() {
  if (!_drawerCustomer) return;
  const msg = prompt(`Message to ${_drawerCustomer.phone}:\nTip: use {name} and {business}`,
    `Hi {name}! A quick message from {business} 😊`);
  if (!msg) return;
  try {
    const r = await apiFetch(ROUTES.campaigns, {
      method: 'POST',
      body: JSON.stringify({ audience: 'custom', message: msg, phone_list: [_drawerCustomer.phone] })
    });
    toast(r.sent ? '✅ Message sent!' : '⚠ Failed to send', r.sent === 0);
  } catch (e) { toast(e.message, true); }
}


// ════════════════════════════════════════════════════════════════════════════
// PHASE 4 (cont.) — PAYMENT REMINDERS SECTION
// ════════════════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════════════
// BOOKINGS — native appointment/scheduling module (Growth plan feature)
// Backend endpoints (already existed, now Growth-gated server-side):
//   GET    /bookings                  — list
//   POST   /bookings                  — create
//   PATCH  /bookings/{id}/status      — confirm/complete/cancel/no-show
//   DELETE /bookings/{id}             — cancel
//   GET    /bookings/availability     — check a slot before creating
//   GET/PATCH /me/service-mode        — working hours, slot length, lead time
// A 403 plan_required response is already handled globally by apiFetch(),
// which shows the existing upgrade modal — this section just needs a
// lightweight in-page fallback for Starter users landing here directly.
// ═══════════════════════════════════════════════════════════════════════════
let _allBookings = [];
let _bkView = 'list';           // 'list' | 'calendar'
let _bkCalMode = 'month';       // 'month' | 'week' | 'agenda'
let _bkCalDate = new Date();    // date currently focused in the calendar
let _bkFilters = { status: 'all', payment: 'all', search: '', dateFrom: '', dateTo: '' };

// Bookings list is small enough per-tenant to load in full (same assumption
// the original loadBookings() already made with upcoming_only=false) —
// filters below run client-side over _allBookings rather than re-fetching
// on every keystroke. The backend's GET /bookings still accepts the same
// filters as real query params (date_from/date_to/status/service/customer/
// payment_status) for any caller that needs server-side filtering at scale
// (e.g. a future paginated view); this dashboard doesn't need it yet.
async function loadBookings() {
  const wrap = document.getElementById('bookings-content');
  if (!wrap) return;
  // BUG FIX: this used to do `wrap.innerHTML = '<div class="panel full">…Loading…</div>'`
  // on first load, which replaced the ENTIRE bookings panel structure (stat
  // cards, table, calendar, settings — everything with the ids the render
  // functions below look up by getElementById). Once the fetch resolved,
  // nothing ever put the real structure back, so `_renderBookingsSummary()`
  // /`_renderBookingsTable()` silently no-op'd (every lookup is `if (el)`
  // guarded) and the section was stuck on "Loading…" forever — on exactly
  // the first visit per page session, since after that `_allBookings.length`
  // is already > 0 and the wipe doesn't re-trigger. This reproduces the
  // reported "bookings not loading" symptom on the frontend independently
  // of the backend error-visibility bug fixed in booking_service.py. Fixed
  // by only touching the table body for the loading placeholder, never the
  // panel structure itself.
  if (!_allBookings.length) {
    const tbody0 = document.getElementById('bookings-table-body');
    if (tbody0) tbody0.innerHTML = '<tr><td colspan="6"><div class="empty">Loading…</div></td></tr>';
  }
  try {
    const data = await apiFetch('/bookings?upcoming_only=false');
    if (!data) {
      // apiFetch already showed the upgrade modal for a plan_required 403.
      // Show a lightweight in-page explanation too, since the modal can be
      // dismissed and the section would otherwise look blank/broken.
      wrap.innerHTML = `
        <div class="panel full" style="text-align:center;padding:48px 24px;">
          <div style="font-size:40px;margin-bottom:14px;">🗓️</div>
          <h2 style="font-size:20px;font-weight:800;margin-bottom:10px;">Let customers book you online</h2>
          <p style="font-family:var(--mono);font-size:13px;color:var(--text-dim);
                    max-width:420px;margin:0 auto 22px;line-height:1.7;">
            WaziBot Bookings is available on Growth. Give your customers an easy way to
            schedule appointments through WhatsApp and your booking link.
          </p>
          <a href="/pricing" class="btn btn-purple" style="display:inline-block;text-decoration:none;">
            Upgrade to Growth →
          </a>
        </div>`;
      return;
    }
    _allBookings = data.bookings || [];
    applyBookingFilters();
    wrap.style.display = '';
    loadBookingSettings();
  } catch (e) {
    wrap.innerHTML = `<div class="panel full">${_errorBlock('bookings', 'loadBookings')}</div>`;
  }
}

function _renderBookingsSummary(bookings) {
  const todayStr = new Date().toISOString().slice(0, 10);
  const today      = bookings.filter(b => b.booking_date === todayStr && b.status !== 'cancelled').length;
  const upcoming   = bookings.filter(b => b.booking_date > todayStr && ['confirmed','pending','rescheduled'].includes(b.status)).length;
  const completed  = bookings.filter(b => b.status === 'completed').length;
  const cancelled  = bookings.filter(b => b.status === 'cancelled').length;

  const _s = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  _s('bk-stat-today', today);
  _s('bk-stat-upcoming', upcoming);
  _s('bk-stat-completed', completed);
  _s('bk-stat-cancelled', cancelled);

  const next = bookings
    .filter(b => b.booking_date >= todayStr && ['confirmed','pending','rescheduled'].includes(b.status))
    .sort((a, b) => (a.booking_date + a.start_time).localeCompare(b.booking_date + b.start_time))[0];
  const nextEl = document.getElementById('bk-next-appt');
  if (nextEl) {
    nextEl.textContent = next
      ? `${next.customer_phone || 'Customer'} — ${next.service_name || 'Appointment'} — ${next.booking_date} ${next.start_time}`
      : 'No upcoming appointments';
  }
}

const _BK_STATUS_BADGE = {
  pending:     'badge-amber',
  confirmed:   'badge-green',
  completed:   'badge-purple',
  cancelled:   'badge-red',
  no_show:     'badge-red',
  rescheduled: 'badge-amber',
};

function _renderBookingsTable(bookings) {
  const tbody = document.getElementById('bookings-table-body');
  if (!tbody) return;
  const sorted = [...bookings].sort((a, b) => (b.booking_date + b.start_time).localeCompare(a.booking_date + a.start_time));
  if (!sorted.length) {
    tbody.innerHTML = `<tr><td colspan="6"><div class="empty">No bookings match these filters.</div></td></tr>`;
    return;
  }
  tbody.innerHTML = sorted.map(b => `
    <tr style="cursor:pointer;" onclick="openBookingDetail(${b.id})" title="Click to view/edit">
      <td>${escHtml(b.booking_date || '')}<br/><span style="color:var(--text-dim);font-size:11px;">${escHtml(b.start_time || '')}</span></td>
      <td>${escHtml(b.customer_name || b.customer_phone || '—')}${b.customer_name ? `<br/><span style="color:var(--text-dim);font-size:11px;">${escHtml(b.customer_phone || '')}</span>` : ''}</td>
      <td>${escHtml(b.service_name || '—')}</td>
      <td><span class="badge ${_BK_STATUS_BADGE[b.status] || 'badge-amber'}">${escHtml((b.status||'pending').replace('_',' '))}</span></td>
      <td style="color:var(--text-dim);font-size:11px;">${escHtml(b.notes || '')}</td>
      <td>
        <div style="display:flex;gap:6px;flex-wrap:wrap;" onclick="event.stopPropagation()">
          ${b.status !== 'completed' && b.status !== 'cancelled' ? `
            <button class="btn btn-ghost" style="color:var(--green);" onclick="_bkSetStatus(${b.id},'completed')">✓ Done</button>
            <button class="btn btn-ghost" style="color:var(--amber);" onclick="_bkSetStatus(${b.id},'no_show')">No-show</button>
            <button class="btn btn-ghost" onclick="_bkCancel(${b.id})">✕ Cancel</button>
          ` : ''}
          <button class="btn btn-ghost" onclick="openBookingDetail(${b.id})">✎ Edit</button>
        </div>
      </td>
    </tr>`).join('');
}

// ── Filters (client-side over the already-fully-loaded _allBookings; see
//    loadBookings() note above on why this doesn't hit the server per
//    keystroke) ─────────────────────────────────────────────────────────
function filterBookingsByStatus(v)  { _bkFilters.status  = v; applyBookingFilters(); }
function filterBookingsByPayment(v) { _bkFilters.payment = v; applyBookingFilters(); }
function filterBookingsBySearch(v)  { _bkFilters.search  = (v || '').trim().toLowerCase(); applyBookingFilters(); }
function filterBookingsByDateRange() {
  _bkFilters.dateFrom = document.getElementById('bk-date-from')?.value || '';
  _bkFilters.dateTo   = document.getElementById('bk-date-to')?.value || '';
  applyBookingFilters();
}

function _bookingMatchesFilters(b) {
  const f = _bkFilters;
  if (f.status !== 'all' && (b.status || 'pending') !== f.status) return false;
  if (f.payment !== 'all' && (b.payment_status || '') !== f.payment) return false;
  if (f.dateFrom && (b.booking_date || '') < f.dateFrom) return false;
  if (f.dateTo && (b.booking_date || '') > f.dateTo) return false;
  if (f.search) {
    const hay = `${b.customer_name || ''} ${b.customer_phone || ''}`.toLowerCase();
    if (!hay.includes(f.search)) return false;
  }
  return true;
}

// Stats/"Next Appointment" always reflect ALL of this business's bookings
// (matches the original, pre-filter behavior); only the table and calendar
// narrow down to the active filters.
function applyBookingFilters() {
  _renderBookingsSummary(_allBookings);
  const filtered = _allBookings.filter(_bookingMatchesFilters);
  _renderBookingsTable(filtered);
  if (_bkView === 'calendar') _renderBookingsCalendar(filtered);
}

// ── List / Calendar view toggle ──────────────────────────────────────────
function setBookingsView(mode) {
  _bkView = mode;
  const listV = document.getElementById('bookings-list-view');
  const calV  = document.getElementById('bookings-calendar-view');
  const listBtn = document.getElementById('bk-view-list');
  const calBtn  = document.getElementById('bk-view-calendar');
  if (listV) listV.style.display = mode === 'list' ? '' : 'none';
  if (calV)  calV.style.display  = mode === 'calendar' ? '' : 'none';
  if (listBtn) listBtn.style.opacity = mode === 'list' ? '1' : '0.4';
  if (calBtn)  calBtn.style.opacity  = mode === 'calendar' ? '1' : '0.4';
  if (mode === 'calendar') _renderBookingsCalendar(_allBookings.filter(_bookingMatchesFilters));
}

// ── Calendar (Month / Week / Agenda) ─────────────────────────────────────
// Additive to the existing panel styling — reuses .panel/.badge/.btn-ghost
// classes rather than introducing a new visual language. Drag/resize was
// evaluated and skipped: this codebase has no existing drag-and-drop
// infrastructure anywhere in the dashboard, so wiring it up safely (pointer
// events, backend re-validation, touch support down to 360px) would be a
// substantial net-new subsystem rather than an incremental addition — per
// the spec's own instruction ("If drag/resize would require a major
// rewrite, do not implement it"), booking moves go through the existing
// click → edit-modal → save flow instead, which already re-validates
// availability server-side via update_booking()/reschedule_booking().
function setBookingsCalMode(mode) {
  _bkCalMode = mode;
  ['month', 'week', 'agenda'].forEach(m => {
    const btn = document.getElementById(`bk-cal-mode-${m}`);
    if (btn) btn.style.opacity = m === mode ? '1' : '0.5';
  });
  _renderBookingsCalendar(_allBookings.filter(_bookingMatchesFilters));
}

function bkCalNav(dir) {
  if (dir === 0) {
    _bkCalDate = new Date();
  } else if (_bkCalMode === 'month') {
    _bkCalDate = new Date(_bkCalDate.getFullYear(), _bkCalDate.getMonth() + dir, 1);
  } else if (_bkCalMode === 'week') {
    _bkCalDate = new Date(_bkCalDate.getTime() + dir * 7 * 86400000);
  } else {
    _bkCalDate = new Date(_bkCalDate.getTime() + dir * 86400000);
  }
  _renderBookingsCalendar(_allBookings.filter(_bookingMatchesFilters));
}

function _bkDateStr(d) {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function _renderBookingsCalendar(bookings) {
  const body = document.getElementById('bk-calendar-body');
  const title = document.getElementById('bk-cal-title');
  if (!body) return;

  const byDate = {};
  for (const b of bookings) {
    const d = b.booking_date;
    if (!d) continue;
    (byDate[d] = byDate[d] || []).push(b);
  }
  for (const d in byDate) byDate[d].sort((a, b) => (a.start_time || '').localeCompare(b.start_time || ''));

  const todayStr = _bkDateStr(new Date());

  const chip = (b) => `
    <div class="bk-cal-chip bk-cal-chip-${_BK_STATUS_BADGE[b.status] || 'badge-amber'}"
         onclick="event.stopPropagation();openBookingDetail(${b.id})"
         title="${escHtml((b.customer_name || b.customer_phone || 'Booking'))} — ${escHtml(b.service_name || '')}">
      ${escHtml(b.start_time || '')} ${escHtml(b.customer_name || b.customer_phone || 'Booking')}
    </div>`;

  if (_bkCalMode === 'agenda') {
    if (title) title.textContent = 'Agenda — upcoming bookings';
    const days = Object.keys(byDate).filter(d => d >= todayStr).sort();
    body.innerHTML = days.length ? days.map(d => `
      <div class="bk-cal-agenda-day">
        <div class="bk-cal-agenda-date">${d === todayStr ? 'Today · ' : ''}${escHtml(d)}</div>
        <div>${byDate[d].map(chip).join('')}</div>
      </div>`).join('') : '<div class="empty">No upcoming bookings.</div>';
    return;
  }

  if (_bkCalMode === 'week') {
    const start = new Date(_bkCalDate);
    start.setDate(start.getDate() - start.getDay());
    const days = [...Array(7)].map((_, i) => new Date(start.getTime() + i * 86400000));
    if (title) title.textContent = `Week of ${_bkDateStr(days[0])}`;
    body.innerHTML = `<div class="bk-cal-grid bk-cal-grid-week">` + days.map(d => {
      const ds = _bkDateStr(d);
      const items = byDate[ds] || [];
      return `<div class="bk-cal-cell ${ds === todayStr ? 'bk-cal-today' : ''}" onclick="_bkCalEmptyClick('${ds}')">
        <div class="bk-cal-daynum">${d.toLocaleDateString(undefined,{weekday:'short'})} ${d.getDate()}</div>
        <div class="bk-cal-chips">${items.map(chip).join('') || ''}</div>
      </div>`;
    }).join('') + `</div>`;
    return;
  }

  // month (default)
  const first = new Date(_bkCalDate.getFullYear(), _bkCalDate.getMonth(), 1);
  const gridStart = new Date(first);
  gridStart.setDate(gridStart.getDate() - gridStart.getDay());
  if (title) title.textContent = first.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  const cells = [...Array(42)].map((_, i) => new Date(gridStart.getTime() + i * 86400000));
  const dow = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  body.innerHTML = `
    <div class="bk-cal-grid bk-cal-grid-header">${dow.map(d => `<div class="bk-cal-dow">${d}</div>`).join('')}</div>
    <div class="bk-cal-grid bk-cal-grid-month">` + cells.map(d => {
      const ds = _bkDateStr(d);
      const items = byDate[ds] || [];
      const otherMonth = d.getMonth() !== first.getMonth();
      const shown = items.slice(0, 3);
      const more = items.length - shown.length;
      return `<div class="bk-cal-cell ${otherMonth ? 'bk-cal-other-month' : ''} ${ds === todayStr ? 'bk-cal-today' : ''}" onclick="_bkCalEmptyClick('${ds}')">
        <div class="bk-cal-daynum">${d.getDate()}</div>
        <div class="bk-cal-chips">${shown.map(chip).join('')}${more > 0 ? `<div class="bk-cal-more">+${more} more</div>` : ''}</div>
      </div>`;
    }).join('') + `</div>`;
}

// Clicking an empty calendar slot opens the create-booking modal prefilled
// with that date, per the spec ("click empty slot → create").
function _bkCalEmptyClick(dateStr) {
  openCreateBookingModal();
  const dateInput = document.getElementById('bk-new-date');
  if (dateInput) dateInput.value = dateStr;
}

// ── CSV export ────────────────────────────────────────────────────────
async function exportBookingsCsv() {
  try {
    const params = new URLSearchParams({ upcoming_only: 'false' });
    if (_bkFilters.status !== 'all') params.set('status', _bkFilters.status);
    if (_bkFilters.dateFrom) params.set('date_from', _bkFilters.dateFrom);
    if (_bkFilters.dateTo) params.set('date_to', _bkFilters.dateTo);
    const res = await fetch(`${API}/bookings/export?${params.toString()}`, {
      headers: { 'Authorization': `Bearer ${token}` },
    });
    if (!res.ok) {
      let msg = res.statusText;
      try { const e = await res.json(); msg = e.detail || msg; } catch {}
      throw new Error(msg);
    }
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = Object.assign(document.createElement('a'), { href: url, download: 'bookings.csv' });
    a.click();
    URL.revokeObjectURL(url);
    toast('📥 Bookings exported as CSV');
  } catch (e) {
    toast('Failed to export bookings: ' + e.message, true);
  }
}

async function _bkSetStatus(id, status) {
  try {
    await apiFetch(`/bookings/${id}/status?status=${status}`, { method: 'PATCH' });
    toast('Booking updated ✅');
    loadBookings();
  } catch (e) { toast('Failed to update booking: ' + e.message, true); }
}

async function _bkCancel(id) {
  if (!confirm('Cancel this booking?')) return;
  try {
    await apiFetch(`/bookings/${id}`, { method: 'DELETE' });
    toast('Booking cancelled');
    loadBookings();
  } catch (e) { toast('Failed to cancel booking: ' + e.message, true); }
}

function openCreateBookingModal() {
  const modal = document.getElementById('create-booking-modal');
  if (modal) modal.classList.add('open');
}
function closeCreateBookingModal() {
  const modal = document.getElementById('create-booking-modal');
  if (modal) modal.classList.remove('open');
}

async function submitCreateBooking() {
  const phone = document.getElementById('bk-new-phone')?.value.trim();
  const name  = document.getElementById('bk-new-name')?.value.trim() || '';
  const date  = document.getElementById('bk-new-date')?.value;
  const time  = document.getElementById('bk-new-time')?.value;
  const dur   = parseFloat(document.getElementById('bk-new-duration')?.value || '1');
  const svc   = document.getElementById('bk-new-service')?.value.trim() || '';
  const notes = document.getElementById('bk-new-notes')?.value.trim() || '';

  if (!phone || !date || !time) { toast('Phone, date, and time are required', true); return; }

  try {
    await apiFetch('/bookings', {
      method: 'POST',
      body: JSON.stringify({
        customer_phone: phone, customer_name: name || undefined,
        booking_date: date, start_time: time,
        duration_hrs: dur, service_name: svc, notes: notes,
      }),
    });
    toast('✅ Booking created');
    closeCreateBookingModal();
    loadBookings();
  } catch (e) {
    // The backend returns 409 with a friendly message when the slot was
    // just taken by someone else — surfaced as-is rather than a generic error.
    toast(e.message || 'Failed to create booking', true);
  }
}

// ── Booking Detail / Edit modal ──────────────────────────────────────────
let _bkDetailId = null;

function openBookingDetail(id) {
  const b = _allBookings.find(x => x.id === id);
  if (!b) { toast('Booking not found', true); return; }
  _bkDetailId = id;
  const _v = (elId, val) => { const el = document.getElementById(elId); if (el) el.value = val ?? ''; };
  _v('bk-edit-id', b.id);
  _v('bk-edit-phone', b.customer_phone);
  _v('bk-edit-name', b.customer_name);
  _v('bk-edit-service', b.service_name);
  _v('bk-edit-date', b.booking_date);
  _v('bk-edit-time', b.start_time);
  _v('bk-edit-duration', b.duration_hrs ?? 1);
  _v('bk-edit-status', b.status || 'pending');
  _v('bk-edit-price', b.price);
  _v('bk-edit-payment-status', b.payment_status || '');
  _v('bk-edit-notes', b.notes);
  const errEl = document.getElementById('bk-edit-error');
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
  const modal = document.getElementById('booking-detail-modal');
  if (modal) modal.classList.add('open');
}

function closeBookingDetailModal() {
  const modal = document.getElementById('booking-detail-modal');
  if (modal) modal.classList.remove('open');
  _bkDetailId = null;
}

async function submitBookingEdit() {
  if (!_bkDetailId) return;
  const errEl = document.getElementById('bk-edit-error');
  const setErr = (msg) => { if (errEl) { errEl.textContent = msg; errEl.style.display = msg ? '' : 'none'; } };
  setErr('');

  const original = _allBookings.find(x => x.id === _bkDetailId) || {};
  const payload = {
    customer_phone: document.getElementById('bk-edit-phone')?.value.trim(),
    customer_name:  document.getElementById('bk-edit-name')?.value.trim() || null,
    service_name:   document.getElementById('bk-edit-service')?.value.trim(),
    booking_date:   document.getElementById('bk-edit-date')?.value,
    start_time:     document.getElementById('bk-edit-time')?.value,
    duration_hrs:   parseFloat(document.getElementById('bk-edit-duration')?.value || '1'),
    notes:          document.getElementById('bk-edit-notes')?.value.trim() || '',
    price:          document.getElementById('bk-edit-price')?.value ? parseFloat(document.getElementById('bk-edit-price').value) : null,
    payment_status: document.getElementById('bk-edit-payment-status')?.value || null,
  };
  const newStatus = document.getElementById('bk-edit-status')?.value;

  try {
    // Date/time changed → go through the reschedule endpoint so the new
    // slot gets the same availability re-check WhatsApp reschedules get
    // (excluding this booking's own current row from the conflict check).
    const dateOrTimeChanged = payload.booking_date !== original.booking_date || payload.start_time !== original.start_time
      || payload.duration_hrs !== (original.duration_hrs ?? 1);
    if (dateOrTimeChanged) {
      await apiFetch(`/bookings/${_bkDetailId}/reschedule`, {
        method: 'POST',
        body: JSON.stringify({
          new_date: payload.booking_date, new_time: payload.start_time, duration_hrs: payload.duration_hrs,
        }),
      });
    }
    // Field edits (customer/service/notes/price/payment_status) go through
    // the general PATCH. Status is a SEPARATE existing endpoint
    // (PATCH /bookings/{id}/status?status=…) — the general BookingUpdate
    // model deliberately has no `status` field (kept backward-compatible
    // with the pre-existing status-only route, see update_booking_api()'s
    // own docstring), so it's called on its own, only when changed.
    const { booking_date, start_time, duration_hrs, ...rest } = payload;
    await apiFetch(`/bookings/${_bkDetailId}`, {
      method: 'PATCH',
      body: JSON.stringify(rest),
    });
    if (newStatus && newStatus !== (original.status || 'pending')) {
      await apiFetch(`/bookings/${_bkDetailId}/status?status=${encodeURIComponent(newStatus)}`, { method: 'PATCH' });
    }
    toast('✅ Booking updated');
    closeBookingDetailModal();
    loadBookings();
  } catch (e) {
    setErr(e.message || 'Failed to save changes — the new time may already be booked.');
  }
}

async function _bkCancelFromDetail() {
  if (!_bkDetailId) return;
  if (!confirm('Cancel this booking?')) return;
  try {
    await apiFetch(`/bookings/${_bkDetailId}`, { method: 'DELETE' });
    toast('Booking cancelled');
    closeBookingDetailModal();
    loadBookings();
  } catch (e) { toast('Failed to cancel booking: ' + e.message, true); }
}

async function loadBookingSettings() {
  try {
    const cfg = await apiFetch('/me/service-mode');
    if (!cfg) return;
    const _v = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
    _v('bk-set-slot-mins', cfg.default_slot_mins ?? 60);
    _v('bk-set-lead-hrs', cfg.booking_lead_hrs ?? 1);
    _v('bk-set-hours-start', cfg.working_hours_start ?? '08:00');
    _v('bk-set-hours-end', cfg.working_hours_end ?? '17:00');
  } catch (e) { /* non-critical */ }
}

async function saveBookingSettings() {
  try {
    await apiFetch('/me/service-mode', {
      method: 'PATCH',
      body: JSON.stringify({
        is_service_business: true,
        default_slot_mins:   parseInt(document.getElementById('bk-set-slot-mins')?.value || '60'),
        booking_lead_hrs:    parseInt(document.getElementById('bk-set-lead-hrs')?.value || '1'),
        working_hours_start: document.getElementById('bk-set-hours-start')?.value || '08:00',
        working_hours_end:   document.getElementById('bk-set-hours-end')?.value || '17:00',
      }),
    });
    toast('✅ Booking settings saved');
  } catch (e) { toast('Failed to save: ' + e.message, true); }
}

async function loadReminders() {
  const tbody = document.getElementById('reminders-table-body');
  if (tbody) tbody.innerHTML = '<tr><td colspan="7"><div class="empty">Loading…</div></td></tr>';
  try {
    const data = await apiFetch(ROUTES.reminders);
    const orders = data && data.orders ? data.orders : [];
    let t1=0, t2=0, t3=0;
    orders.forEach(o => { if(o.reminder_tier===3)t3++; else if(o.reminder_tier===2)t2++; else t1++; });
    ['t1','t2','t3'].forEach((t,i) => {
      const el = document.getElementById(`rem-${t}-count`);
      if (el) el.textContent = [t1,t2,t3][i];
    });
    const allEl = document.getElementById('rem-all-count');
    if (allEl) allEl.textContent = orders.length;
    // Nav badge
    const nb = document.getElementById('nav-rem-badge');
    if (nb) { nb.textContent = orders.length; nb.style.display = orders.length > 0 ? 'inline-flex' : 'none'; }

    if (!tbody) return;
    if (!orders.length) { tbody.innerHTML = '<tr><td colspan="7"><div class="empty">✅ No pending payments.</div></td></tr>'; return; }
    const tierColor = { 1:'var(--amber)', 2:'#f97316', 3:'var(--red)' };
    tbody.innerHTML = orders.map(o => {
      const tier = o.reminder_tier || 1;
      const age  = o.created_at ? Math.round((Date.now() - new Date(o.created_at).getTime()) / 3600000) : '?';
      return `<tr>
        <td><span class="badge badge-amber">#${o.order_id||'—'}</span></td>
        <td style="font-family:var(--mono);font-size:11px;">${escHtml(o.customer_phone||'—')}</td>
        <td><span class="badge badge-green">${getCurrencySymbol()}${parseFloat(o.total_price||0).toFixed(2)}</span></td>
        <td style="font-family:var(--mono);font-size:11px;">${escHtml(o.payment_method||'—')}</td>
        <td><span style="color:${tierColor[tier]||'var(--text)'};font-family:var(--mono);font-size:11px;font-weight:700;">Tier ${tier}</span></td>
        <td style="font-family:var(--mono);font-size:11px;">${age}h ago</td>
        <td>
          <button class="btn btn-ghost" style="font-size:11px;padding:4px 8px;" onclick="nudgeOrder(${o.order_id})">📨 Nudge</button>
          <button class="btn btn-ghost" style="font-size:11px;padding:4px 8px;margin-left:4px;" onclick="previewReminder(${o.order_id})">👁</button>
        </td>
      </tr>`;
    }).join('');
  } catch (e) {
    if (tbody) tbody.innerHTML = _errorRow(7, 'reminders', 'loadReminders');
  }
}

async function nudgeOrder(orderId) {
  try {
    const r = await apiFetch(`/payments/reminders/${orderId}/nudge`, { method: 'POST' });
    toast(r.ok ? '📨 Reminder sent!' : ('Failed: ' + r.error), !r.ok);
    loadReminders();
  } catch (e) { toast(e.message, true); }
}

async function previewReminder(orderId) {
  try {
    const r = await apiFetch(`/payments/reminders/${orderId}/preview`);
    if (r && r.preview_message) alert(r.preview_message);
  } catch (e) { toast(e.message, true); }
}


// ════════════════════════════════════════════════════════════════════════════
// PHASE 5 — ORDER KANBAN VIEW
// ════════════════════════════════════════════════════════════════════════════

let _ordersData = [];
let _ordersView = 'list';
let _orderStatusFilter = 'all';

function setOrderView(mode) {
  _ordersView = mode;
  const listV   = document.getElementById('orders-list-view');
  const kanbanV = document.getElementById('orders-kanban-view');
  const listBtn = document.getElementById('orders-view-list');
  const kanbanBtn = document.getElementById('orders-view-kanban');
  if (listV)   listV.style.display   = mode === 'list'   ? '' : 'none';
  if (kanbanV) kanbanV.style.display = mode === 'kanban' ? '' : 'none';
  if (listBtn) listBtn.style.opacity   = mode === 'list'   ? '1' : '0.4';
  if (kanbanBtn) kanbanBtn.style.opacity = mode === 'kanban' ? '1' : '0.4';
  if (mode === 'kanban' && _ordersData.length) renderKanban(_ordersData);
}

function filterOrdersByStatus(status) {
  _orderStatusFilter = status;
  applyOrderListFilters();
}

// UI/UX audit: Orders had a status filter but no search box, even though
// order IDs/phone numbers/product names are already loaded client-side.
// Combines with the existing status filter rather than replacing it.
let _orderSearchQuery = '';
function filterOrdersBySearch(query) {
  _orderSearchQuery = (query || '').trim().toLowerCase();
  applyOrderListFilters();
}

// UI/UX audit: this filter existed once before (renderOrderFilters()/
// applyOrderFilters() below) but the bar that held it was never actually
// attached to the page — it targeted #orders-section/[data-section="orders"]
// which don't exist, so it silently never rendered. Payment-status filtering
// is real, useful, and not covered by the search box above, so it's folded
// into the working toolbar instead of reviving a second, disconnected bar.
let _orderPaymentFilter = 'all';
function filterOrdersByPayment(payment) {
  _orderPaymentFilter = payment;
  applyOrderListFilters();
}

function applyOrderListFilters() {
  let filtered = _orderStatusFilter === 'all' ? _ordersData : _ordersData.filter(o => o.status === _orderStatusFilter);
  if (_orderPaymentFilter !== 'all') {
    filtered = filtered.filter(o => (o.payment_status || 'pending') === _orderPaymentFilter);
  }
  if (_orderSearchQuery) {
    const q = _orderSearchQuery;
    filtered = filtered.filter(o =>
      String(o.id||'').includes(q) ||
      (o.customer_phone||'').toLowerCase().includes(q) ||
      (o.product_name||'').toLowerCase().includes(q)
    );
  }
  renderOrders(filtered, 'orders-body', true);
  if (_ordersView === 'kanban') renderKanban(filtered);
}

// ════════════════════════════════════════════════════════════════════════════
// ORDER DETAIL DRAWER (UI/UX enhancement) — reuses the customer-drawer CSS
// component. Clicking any order row opens this instead of navigating away.
// ════════════════════════════════════════════════════════════════════════════
let _orderDrawerOrder = null;

function openOrderDrawer(orderId) {
  const order = _ordersData.find(o => o.id === orderId);
  if (!order) { toast('Order not found', true); return; }
  _orderDrawerOrder = order;
  const status = order.status || 'pending';
  const labels = window.IS_SERVICE_BUSINESS ? _ORDER_STATUS_LABELS_SERVICE : _ORDER_STATUS_LABELS;
  const keys   = window.IS_SERVICE_BUSINESS ? _ORDER_STATUS_KEYS_SERVICE  : _ORDER_STATUS_KEYS_PRODUCT;

  document.getElementById('order-drawer-id').textContent        = `ORDER #${order.id}`;
  document.getElementById('order-drawer-time').textContent      = fmtTime(order.created_at || order.createdAt || order.timestamp) || '—';
  document.getElementById('order-drawer-customer').textContent  = order.customer_phone || '—';
  document.getElementById('order-drawer-qty').textContent       = order.quantity || 0;
  document.getElementById('order-drawer-total').textContent     = getCurrencySymbol() + parseFloat(order.total_price||0).toFixed(2);
  document.getElementById('order-drawer-status-badge').textContent = labels[status] || status;
  document.getElementById('order-drawer-item').textContent      = order.product_name || '—';

  // UI/UX audit (Phase 9): the drawer showed the item name as plain text
  // with no way to jump to that product (e.g. to check current stock).
  // Matches by name since orders store product_name, not a product_id —
  // same join key Products' own "View Orders" action (viewOrdersForProduct)
  // uses in the other direction. Hidden when no match is found rather than
  // showing a button that would silently no-op.
  const viewProdBtn = document.getElementById('order-drawer-view-product-btn');
  if (viewProdBtn) {
    const matchedProduct = (_allProducts || []).find(p => p.name === order.product_name);
    viewProdBtn.style.display = matchedProduct ? '' : 'none';
  }

  const sel = document.getElementById('order-drawer-status-select');
  sel.innerHTML = keys.filter(k => k !== 'all').map(k => `<option value="${k}">${labels[k]}</option>`).join('');
  sel.value = keys.includes(status) ? status : keys[0];

  document.getElementById('order-drawer').classList.add('open');
  document.getElementById('drawer-overlay').classList.add('open');
  _focusIntoDrawer('order-drawer');
}

function closeOrderDrawer() {
  const d = document.getElementById('order-drawer');
  if (d) d.classList.remove('open');
  if (!document.getElementById('customer-drawer').classList.contains('open')) {
    document.getElementById('drawer-overlay').classList.remove('open');
  }
  _orderDrawerOrder = null;
  _restoreFocusFromDrawer();
}

async function updateOrderStatusFromDrawer() {
  if (!_orderDrawerOrder) return;
  const sel = document.getElementById('order-drawer-status-select');
  const pick = sel.value;
  if (pick === (_orderDrawerOrder.status || 'pending')) { closeOrderDrawer(); return; }
  try {
    await apiFetch(`/orders/${_orderDrawerOrder.id}/status`, { method: 'PUT', body: JSON.stringify({ status: pick }) });
    toast(`✅ Order #${_orderDrawerOrder.id} → ${pick}`);
    closeOrderDrawer();
    await loadOrders();
  } catch (e) { toast('Update failed: ' + e.message, true); }
}

function contactCustomerFromOrderDrawer() {
  if (!_orderDrawerOrder || !_orderDrawerOrder.customer_phone) return;
  window.open('/inbox?phone=' + encodeURIComponent(_orderDrawerOrder.customer_phone), '_blank');
}

function viewProductFromOrderDrawer() {
  if (!_orderDrawerOrder) return;
  const p = (_allProducts || []).find(pr => pr.name === _orderDrawerOrder.product_name);
  if (!p) { toast('Product not found — it may have been renamed or deleted', true); return; }
  closeOrderDrawer();
  showSection('products', null);
  setTimeout(() => openProdEdit(p.id), 300);
}

// Reuses the existing CRM customer drawer rather than building a second one.
function viewCustomerFromOrderDrawer() {
  if (!_orderDrawerOrder || !_orderDrawerOrder.customer_phone) return;
  const phone = _orderDrawerOrder.customer_phone;
  closeOrderDrawer();
  showSection('crm', null);
  const openWhenReady = () => {
    const match = (_crmTableData || []).find(c => c.phone === phone);
    if (match) { openCustomerDrawer(match); }
    else { toast('No CRM record for ' + phone + ' yet', true); }
  };
  // loadCrm() (triggered by showSection above) is async — give it a moment,
  // then fall back to whatever is already cached if it hasn't finished yet.
  setTimeout(openWhenReady, 400);
}

const _KANBAN_COLS = [
  { key: 'pending',          label: 'Pending',       color: 'var(--amber)' },
  { key: 'pending_cash',     label: 'Confirmed/Cash', color: '#84cc16' },
  { key: 'confirmed',        label: 'Confirmed',      color: 'var(--green)' },
  { key: 'preparing',        label: 'Preparing',      color: 'var(--blue)' },
  { key: 'ready',            label: 'Ready',          color: '#a78bfa' },
  { key: 'out_for_delivery', label: 'Delivering',     color: '#f97316' },
  { key: 'completed',        label: 'Completed',      color: 'var(--text-dim)' },
];

function renderKanban(orders) {
  const board = document.getElementById('kanban-board');
  if (!board) return;
  board.innerHTML = _KANBAN_COLS.map(col => {
    const colOrders = orders.filter(o => (o.status||'pending') === col.key);
    const cards = colOrders.length
      ? colOrders.map(o => {
        // UI/UX audit: kanban cards used to open a raw prompt() to change
        // status; now opens the same Order Detail Drawer the list view
        // uses, with a real dropdown, instead of a second status-change UX.
        const paid = (o.payment_status || 'pending') === 'paid';
        return `
        <div class="kanban-card" onclick="openOrderDrawer(${o.id})">
          <div class="kanban-card-id">#${o.id}</div>
          <div class="kanban-card-name">${escHtml(o.customer_phone||'—')}</div>
          <div class="kanban-card-total">${getCurrencySymbol()}${parseFloat(o.total_price||0).toFixed(2)}
            <span style="font-size:10px;font-family:var(--mono);padding:1px 6px;border-radius:4px;margin-left:4px;${paid?'background:rgba(34,197,94,.15);color:#22c55e;':'background:rgba(245,158,11,.15);color:#f59e0b;'}">${paid?'Paid':'Unpaid'}</span>
          </div>
          <div class="kanban-card-time">${fmtTime(o.created_at)}</div>
        </div>`;
        }).join('')
      : '<div class="kanban-empty">—</div>';
    return `
      <div class="kanban-col">
        <div class="kanban-col-header" style="border-top-color:${col.color};">
          <span>${col.label}</span>
          <span class="kanban-col-count">${colOrders.length}</span>
        </div>
        <div class="kanban-col-body">${cards}</div>
      </div>`;
  }).join('');
}

async function updateOrderStatus(orderId) {
  const order = _ordersData.find(o => o.id === orderId);
  if (!order) return;
  const statuses = ['pending','pending_cash','confirmed','preparing','ready','out_for_delivery','delivered','completed','cancelled'];
  const cur  = order.status || 'pending';
  const opts = statuses.map(s => `${s === cur ? '▶ ' : '  '}${s}`).join('\n');
  const pick = prompt(`Update status for ORDER #${orderId}\n\nCurrent: ${cur}\n\nPick new status:\n${opts}\n\nType new status:`);
  if (!pick || pick === cur) return;
  if (!statuses.includes(pick)) { toast('Invalid status', true); return; }
  try {
    await apiFetch(`/orders/${orderId}/status`, { method: 'PUT', body: JSON.stringify({ status: pick }) });
    toast(`✅ ORDER-${orderId} → ${pick}`);
    await loadOrders();
  } catch (e) { toast('Update failed: ' + e.message, true); }
}

// Patch loadOrders to capture data for kanban + filter
// Phase 5 — patch loadOrders to also store _ordersData (IIFE avoids TDZ)
loadOrders = (function(_prev5) {
  return async function() {
  // UI/UX audit: Orders previously showed nothing while the fetch was in
  // flight (stale content lingers until the response arrives) — this gives
  // an immediate, honest "Loading…" row instead of an apparently-frozen UI.
  const _ob = document.getElementById('orders-body');
  if (_ob && !_ordersData.length) _ob.innerHTML = '<tr><td colspan="7"><div class="empty">Loading…</div></td></tr>';
  try {
    const raw = await apiFetch(ROUTES.orders);
    if (!raw) return;
    _ordersData = Array.isArray(raw) ? raw : (Array.isArray(raw.data) ? raw.data : []);
    const filtered = _orderStatusFilter === 'all' ? _ordersData : _ordersData.filter(o => o.status === _orderStatusFilter);
    renderOrders(filtered, 'orders-body', true);
    renderOrders(_ordersData.slice(0,5), 'recent-orders-body', false);
    const statO = document.getElementById('stat-orders');
    const statR = document.getElementById('stat-revenue');
    if (statO) statO.textContent = _ordersData.length;
    if (statR) statR.textContent = getCurrencySymbol() + _ordersData.reduce((s,o)=>s+(o.total_price||0),0).toFixed(2);
    if (_ordersView === 'kanban') renderKanban(_ordersData);
    // loadOverviewExtras() is called by the outer wrapper — not here
  } catch(e) {
    ['orders-body','recent-orders-body'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = _errorRow(7, 'orders', 'loadOrders');
    });
  }
  };
}(loadOrders));


// ════════════════════════════════════════════════════════════════════════════
// PHASE 6 — ANALYTICS CHARTS
// ════════════════════════════════════════════════════════════════════════════

let _analyticsChartsLoading = false;
async function loadAnalyticsCharts() {
  if (!token) return;
  if (_analyticsChartsLoading) return;
  _analyticsChartsLoading = true;
  try {
    // Use Promise.allSettled so one 403 doesn't block the other
    const [statsResult, topCustResult] = await Promise.allSettled([
      apiFetch(ROUTES.analyticsStats),
      apiFetch(ROUTES.analyticsTop + '?limit=5'),
    ]);
    const stats   = statsResult.status   === 'fulfilled' ? statsResult.value   : null;
    const topCust = topCustResult.status === 'fulfilled' ? topCustResult.value : null;

    // Update stat cards if present
    if (stats) {
      const map = {
        'stat-orders':    stats.total_orders,
        'stat-revenue':   stats.total_revenue != null ? getCurrencySymbol() + parseFloat(stats.total_revenue).toFixed(2) : null,
        'stat-ai':        stats.ai_handled,
        'stat-pending':   stats.pending_orders,
      };
      Object.entries(map).forEach(([id, val]) => {
        const el = document.getElementById(id);
        if (el && val != null) el.textContent = val;
      });
    }

    // Top customers mini-chart (horizontal bar using CSS)
    const chartEl = document.getElementById('analytics-top-customers');
    if (chartEl) {
      if (Array.isArray(topCust) && topCust.length) {
        // will render below
      } else {
        // Clear "Loading analytics..." even when data is unavailable
        chartEl.innerHTML = '<div style="font-family:var(--mono);font-size:12px;color:var(--text-dim);padding:8px 0;">No data yet</div>';
      }
    }
    if (chartEl && Array.isArray(topCust) && topCust.length) {
      const max = Math.max(...topCust.map(c => c.order_count || 0), 1);
      chartEl.innerHTML = topCust.map(c => {
        const pct = Math.round(((c.order_count||0) / max) * 100);
        return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
          <span style="font-family:var(--mono);font-size:10px;color:var(--text-dim);width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escHtml(c.phone||'—')}</span>
          <div style="flex:1;background:var(--surface2);border-radius:4px;height:8px;overflow:hidden;">
            <div style="width:${pct}%;background:var(--green);height:100%;border-radius:4px;transition:width 0.5s;"></div>
          </div>
          <span style="font-family:var(--mono);font-size:10px;color:var(--green);width:20px;text-align:right;">${c.order_count||0}</span>
        </div>`;
      }).join('');
    }
  } catch (_) {
  } finally {
    _analyticsChartsLoading = false;
  }
}

// ════════════════════════════════════════════════════════════════════════════
// PHASE 14 — AI CONVERSATION INSIGHTS
// ════════════════════════════════════════════════════════════════════════════

let _aiInsightsLoading = false;
async function loadAIInsights() {
  if (!token) return;
  if (_aiInsightsLoading) return;
  _aiInsightsLoading = true;
  const body = document.getElementById('ai-insights-body');
  try {
    const data = await apiFetch(ROUTES.analyticsInsights + '?days=30');
    if (!body) return;

    if (!data || data.allowed === false) {
      body.innerHTML = `<div class="empty">
        📊 Detailed AI conversation insights are available on the
        <strong>${escHtml(data && data.required_tier || 'Growth')}</strong> plan and above.
        <a href="${escHtml(data && data.upgrade_url || '/pricing')}" target="_blank">Upgrade →</a>
      </div>`;
      return;
    }

    const pct = (n) => `${Math.round((n || 0) * 1000) / 10}%`;
    const money = (n) => getCurrencySymbol() + (n || 0).toFixed(2);
    const avgResp = data.average_response_seconds;
    const avgRespLabel = avgResp == null ? '—'
      : avgResp < 60 ? `${Math.round(avgResp)}s`
      : `${Math.round(avgResp / 60)}m`;

    const statRow = [
      ['Conversations (30d)', data.conversation_count],
      ['Human handoff rate', pct(data.human_handoff_rate)],
      ['AI failed to understand', `${data.failed_conversations} conv.`],
      ['Unknown intent rate', pct(data.unknown_intent_rate)],
      ['Low-confidence rate', pct(data.low_confidence_rate)],
      ['Avg. response time', avgRespLabel],
      ['Abandoned carts', data.abandoned_carts],
      ['Booking conversations (30d)', data.booking_conversations],
      ['AI-generated sales (30d)', `${data.ai_generated_sales_count} · ${money(data.ai_generated_sales_total)}`],
      ['AI cost (30d)', money(data.ai_cost && data.ai_cost.estimated_cost)],
    ];

    const statsHtml = statRow.map(([label, val]) => `
      <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);">
        <span style="color:var(--text-dim);font-size:12px;">${escHtml(label)}</span>
        <span style="font-family:var(--mono);font-weight:600;">${escHtml(String(val))}</span>
      </div>`).join('');

    const questions = (data.top_customer_questions || []);
    const products   = (data.top_products_requested || []);

    const listHtml = (title, items, key) => {
      if (!items.length) return '';
      return `<div style="margin-top:14px;">
        <div style="font-size:11px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px;">${escHtml(title)}</div>
        ${items.map(it => `<div style="font-size:13px;padding:3px 0;">💬 "${escHtml(it[key])}" <span style="color:var(--text-dim);">(${it.count}×)</span></div>`).join('')}
      </div>`;
    };

    body.innerHTML = `
      <div style="display:grid;grid-template-columns:1fr;gap:0;">${statsHtml}</div>
      ${listHtml('Customers frequently ask', questions, 'text')}
      ${listHtml('Customers frequently request', products, 'name')}
    `;
  } catch (e) {
    if (body) body.innerHTML = `<div class="empty">⚠ ${escHtml(e.message || 'Could not load insights')}</div>`;
  } finally {
    _aiInsightsLoading = false;
  }
}

// Hook analytics load into overview — only when logged in
document.addEventListener('DOMContentLoaded', () => {
  setTimeout(() => { if (token) loadAnalyticsCharts(); }, 500);
  setTimeout(() => { if (token) loadAIInsights(); }, 600);
  // Fetch public config (Supabase URL/key for image uploads)
  fetch('/config/public').then(r => r.json()).then(cfg => {
    window._SUPABASE_URL      = cfg.supabase_url      || '';
    window._SUPABASE_ANON_KEY = cfg.supabase_anon_key || '';
  }).catch(() => {});
});


// ════════════════════════════════════════════════════════════════════════════
// PHASE 7 — TEMPLATE PICKER IN SETTINGS
// ════════════════════════════════════════════════════════════════════════════

async function loadTemplates() {
  const container = document.getElementById('template-picker-options');
  if (!container) return;
  try {
    const tpls = await apiFetch(ROUTES.templates);
    if (!Array.isArray(tpls)) return;
    container.innerHTML = tpls.map(t =>
      `<div class="template-card" data-id="${escHtml(t.id)}" onclick="selectTemplate('${escHtml(t.id)}', this)">
        <div class="template-icon">${t.icon || '🏪'}</div>
        <div class="template-name">${escHtml(t.name)}</div>
      </div>`
    ).join('') +
    `<div class="template-card" data-id="default" onclick="selectTemplate('default', this)">
      <div class="template-icon">🏪</div>
      <div class="template-name">General</div>
    </div>`;

    // Highlight saved template
    const saved = localStorage.getItem('wazi_template_id');
    if (saved) {
      const card = container.querySelector(`[data-id="${saved}"]`);
      if (card) card.classList.add('selected');
    }
  } catch (_) {}
}

function selectTemplate(id, el) {
  document.querySelectorAll('.template-card').forEach(c => c.classList.remove('selected'));
  if (el) el.classList.add('selected');
  localStorage.setItem('wazi_template_id', id);
  toast('✅ Template saved — AI will use category suggestions for this business type.');
}


// ════════════════════════════════════════════════════════════════════════════
// SIMPLE BROADCAST (replaces complex campaign builder)
// ════════════════════════════════════════════════════════════════════════════

const _QUICK_TPLS = {
  promo:    '🔥 Special offer today! Reply *menu* to see what\'s on. Don\'t miss out!',
  restock:  '📦 New stock just arrived! Type *menu* to see what\'s available now.',
  winback:  '😊 Hey! We haven\'t seen you in a while. We\'d love to have you back — type *menu* to order!',
  thankyou: '🙏 Thank you so much for your support! You mean a lot to us. Type *menu* to order anytime.',
};

function quickTpl(key) {
  const ta = document.getElementById('broadcast-msg');
  if (ta) { ta.value = _QUICK_TPLS[key] || ''; updatePreview(); }
}

// Global state for the campaign customer picker (separate from the
// existing allCustomerData/customerPhones used by the legacy recipient
// filter panel, to avoid any collision).
let _bcAllCustomers = [];       // [{phone, customer_name, order_count, total_spent, last_seen}]
let _bcSelectedPhones = new Set();

function onBcAudienceChange() {
  const sel  = document.querySelector('input[name="bc-audience"]:checked');
  const val  = sel ? sel.value : 'all';
  const lbl  = document.getElementById('bc-audience-count');
  const map  = {
    all:          'all customers',
    inactive_30d: 'customers inactive 30+ days',
    vip:          'VIP customers only',
    new:          'new customers only',
    unpaid:       'customers with unpaid orders',
    custom:       'selected customers',
  };
  if (lbl) lbl.textContent = map[val] || val;

  const picker = document.getElementById('bc-customer-picker');
  if (picker) picker.style.display = (val === 'custom') ? 'block' : 'none';

  if (val === 'custom') {
    if (!_bcAllCustomers.length) loadBcCustomerPicker();
    else renderCustomerPicker();
  }

  updatePreview();
  updateBcRecipientPreview(val);
}

// Fetch the full customer list (phone + name) for the picker and the
// non-custom audience preview lists. Uses /crm/segments/all which already
// returns customer_name — more efficient than the legacy /customers +
// /analytics/top-customers double-fetch.
async function loadBcCustomerPicker() {
  const list = document.getElementById('bc-picker-list');
  if (list) list.innerHTML = '<div style="font-family:var(--mono);font-size:12px;color:var(--text-dim);padding:8px;">Loading customers…</div>';
  try {
    const data = await apiFetch(ROUTES.crmSegments + '/all');
    _bcAllCustomers = Array.isArray(data) ? data : [];
    renderCustomerPicker();
    const sel = document.querySelector('input[name="bc-audience"]:checked');
    if (sel && sel.value === 'custom') updateBcRecipientPreview('custom');
  } catch (e) {
    if (list) list.innerHTML = `<div style="font-family:var(--mono);font-size:12px;color:var(--red);padding:8px;">⚠ ${e.message}</div>`;
  }
}

function renderCustomerPicker() {
  const list   = document.getElementById('bc-picker-list');
  const search = (document.getElementById('bc-picker-search') || {}).value || '';
  if (!list) return;

  const q = search.trim().toLowerCase();
  const filtered = _bcAllCustomers.filter(c => {
    if (!q) return true;
    return (c.phone || '').toLowerCase().includes(q) ||
           (c.customer_name || '').toLowerCase().includes(q);
  });

  if (!filtered.length) {
    list.innerHTML = '<div style="font-family:var(--mono);font-size:12px;color:var(--text-dim);padding:8px;">No customers found.</div>';
    return;
  }

  list.innerHTML = filtered.map(c => {
    const checked = _bcSelectedPhones.has(c.phone) ? 'checked' : '';
    const label   = c.customer_name ? `${escHtml(c.customer_name)} — ${escHtml(c.phone)}` : escHtml(c.phone);
    return `
      <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;cursor:pointer;font-family:var(--mono);font-size:12px;"
             onmouseover="this.style.background='var(--surface)'" onmouseout="this.style.background='transparent'">
        <input type="checkbox" data-bc-phone="${escHtml(c.phone)}" ${checked}
               onchange="toggleBcCustomer('${escHtml(c.phone)}', this.checked)"
               style="cursor:pointer;"/>
        <span>${label}</span>
        ${c.order_count ? `<span style="margin-left:auto;color:var(--text-dim);font-size:10px;">${c.order_count} orders</span>` : ''}
      </label>`;
  }).join('');
}

function toggleBcCustomer(phone, checked) {
  if (checked) _bcSelectedPhones.add(phone);
  else _bcSelectedPhones.delete(phone);
  updateBcRecipientPreview('custom');
}

function bcSelectAll(selectAll) {
  const search = (document.getElementById('bc-picker-search') || {}).value || '';
  const q = search.trim().toLowerCase();
  const visible = _bcAllCustomers.filter(c => {
    if (!q) return true;
    return (c.phone || '').toLowerCase().includes(q) ||
           (c.customer_name || '').toLowerCase().includes(q);
  });
  visible.forEach(c => {
    if (selectAll) _bcSelectedPhones.add(c.phone);
    else _bcSelectedPhones.delete(c.phone);
  });
  renderCustomerPicker();
  updateBcRecipientPreview('custom');
}

// Compute and render the live recipient list in the right-side Preview
// panel for whichever audience is currently selected. Shows name (or phone
// if no name) for every recipient, not just a count.
function updateBcRecipientPreview(audience) {
  const recipientListEl = document.getElementById('bc-recipient-list');
  const statSelected     = document.getElementById('stat-selected');
  if (!recipientListEl) return;

  if (audience === 'custom') {
    const chosen = _bcAllCustomers.filter(c => _bcSelectedPhones.has(c.phone));
    if (statSelected) statSelected.textContent = chosen.length;
    recipientListEl.innerHTML = chosen.length
      ? chosen.map(c => `<div style="font-family:var(--mono);font-size:11px;color:var(--text-dim);padding:3px 0;">${escHtml(c.customer_name || c.phone)}</div>`).join('')
      : '<div style="font-family:var(--mono);font-size:11px;color:var(--text-dim);">No customers selected yet.</div>';
    return;
  }

  // Non-custom audiences: fetch the matching segment so the user sees the
  // actual recipients (names/phones), not just a vague count.
  recipientListEl.innerHTML = '<div style="font-family:var(--mono);font-size:11px;color:var(--text-dim);">Loading recipients…</div>';
  if (statSelected) statSelected.textContent = '…';

  const fetchPromise = (() => {
    if (audience === 'all')          return apiFetch(ROUTES.crmSegments + '/all');
    if (audience === 'vip')          return apiFetch(ROUTES.crmSegments + '/vip');
    if (audience === 'new')          return apiFetch(ROUTES.crmSegments + '/new');
    if (audience === 'inactive_30d') return apiFetch(ROUTES.crmInactive + '?days=30');
    if (audience === 'unpaid')       return apiFetch(ROUTES.reminders).then(r => (r && r.orders) || []);
    return Promise.resolve([]);
  })();

  fetchPromise.then(rows => {
    const list = Array.isArray(rows) ? rows : [];
    if (statSelected) statSelected.textContent = list.length;
    if (!list.length) {
      recipientListEl.innerHTML = '<div style="font-family:var(--mono);font-size:11px;color:var(--text-dim);">No customers match this audience.</div>';
      return;
    }
    recipientListEl.innerHTML = list.map(c => {
      const display = c.customer_name || c.customer_phone || c.phone || '—';
      return `<div style="font-family:var(--mono);font-size:11px;color:var(--text-dim);padding:3px 0;">${escHtml(display)}</div>`;
    }).join('');
  }).catch(e => {
    recipientListEl.innerHTML = `<div style="font-family:var(--mono);font-size:11px;color:var(--red);">⚠ ${e.message}</div>`;
    if (statSelected) statSelected.textContent = '—';
  });
}

async function sendBroadcastSimple() {
  const ta  = document.getElementById('broadcast-msg');
  const msg = ta ? ta.value.trim() : '';
  if (!msg) { toast('Write a message first', true); return; }

  const sel      = document.querySelector('input[name="bc-audience"]:checked');
  const audience = sel ? sel.value : 'all';
  const result   = document.getElementById('broadcast-result');
  const btn      = document.getElementById('broadcast-send-btn');

  const audienceLabels = {
    all:          'all customers',
    inactive_30d: 'customers inactive for 30+ days',
    vip:          'VIP customers',
    new:          'new customers',
    unpaid:       'customers with unpaid orders',
    custom:       'the selected customers',
  };

  // Validate custom selection before sending
  if (audience === 'custom') {
    if (!_bcSelectedPhones.size) {
      toast('Select at least one customer first', true);
      return;
    }
  }

  const confirmLabel = audience === 'custom'
    ? `${_bcSelectedPhones.size} selected customer${_bcSelectedPhones.size !== 1 ? 's' : ''}`
    : audienceLabels[audience] || audience;
  if (!confirm(`Send to ${confirmLabel}?`)) return;

  if (btn) btn.disabled = true;
  if (result) result.style.display = 'none';

  try {
    // Use campaign API for audience targeting, fall back to broadcast for "all"
    let r;
    if (audience === 'all') {
      r = await apiFetch(ROUTES.broadcast, {
        method: 'POST',
        body: JSON.stringify({ message: msg }),
      });
    } else if (audience === 'custom') {
      r = await apiFetch(ROUTES.campaigns, {
        method: 'POST',
        body: JSON.stringify({
          audience: 'custom',
          message: msg,
          phone_list: Array.from(_bcSelectedPhones),
          dry_run: false,
        }),
      });
    } else {
      r = await apiFetch(ROUTES.campaigns, {
        method: 'POST',
        body: JSON.stringify({ audience, message: msg, dry_run: false }),
      });
    }

    if (result) {
      result.style.display = 'block';
      const ok = (r.failed || 0) === 0;
      result.className = 'broadcast-result ' + (ok ? 'success' : 'error');
      result.innerHTML = ok
        ? `✅ Sent to <strong>${r.sent}</strong> customer${r.sent !== 1 ? 's' : ''}!`
        : `Sent: <strong>${r.sent}</strong> &nbsp; Failed: <strong>${r.failed}</strong>`;
    }
    toast(`📢 Message sent to ${r.sent || 0} customers!`);
    const statLast = document.getElementById('stat-last');
    if (statLast) statLast.textContent = 'Just now';
    if (ta) ta.value = '';
    updatePreview();
    // reset audience + selection back to "all"
    _bcSelectedPhones.clear();
    const allRadio = document.querySelector('input[name="bc-audience"][value="all"]');
    if (allRadio) { allRadio.checked = true; onBcAudienceChange(); }
  } catch (e) {
    if (result) { result.style.display='block'; result.className='broadcast-result error'; result.textContent='❌ '+e.message; }
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}



/* ══════════════════════════════════════════════════════════
   PHASE 4 — ORDER OPERATIONS ENHANCEMENTS
   Order age, SLA alerts, bulk actions, advanced filters.
   All additive — existing loadOrders/renderOrders unchanged.
══════════════════════════════════════════════════════════ */

// ── Order age helpers ─────────────────────────────────────

function orderAgeMinutes(createdAt) {
  if (!createdAt) return 0;
  return (Date.now() - new Date(createdAt).getTime()) / 60000;
}

function formatOrderAge(createdAt) {
  const mins = orderAgeMinutes(createdAt);
  if (mins < 60)  return `${Math.round(mins)}m`;
  if (mins < 1440) return `${Math.floor(mins/60)}h ${Math.round(mins%60)}m`;
  return `${Math.floor(mins/1440)}d`;
}

function orderAgeClass(createdAt, status) {
  // Only alert on active (not completed/cancelled) orders
  const done = ['completed','delivered','cancelled','refunded'];
  if (done.includes((status||'').toLowerCase())) return 'age-ok';
  const mins = orderAgeMinutes(createdAt);
  if (mins > 120) return 'age-alert';  // > 2 hours
  if (mins > 45)  return 'age-warn';   // > 45 min
  return 'age-ok';
}

// ── SLA alert bar ─────────────────────────────────────────

function renderSlaAlert(orders) {
  const active   = orders.filter(o => !['completed','delivered','cancelled','refunded'].includes((o.status||'').toLowerCase()));
  const overdue  = active.filter(o => orderAgeMinutes(o.created_at) > 120);

  let bar = document.getElementById('sla-alert-bar');
  if (!bar) {
    // Create and insert before kanban/orders table
    bar = document.createElement('div');
    bar.id = 'sla-alert-bar';
    bar.className = 'sla-alert-bar';
    const container = document.getElementById('orders-kanban-view') || document.querySelector('#orders-section .card');
    if (container) container.insertAdjacentElement('afterbegin', bar);
  }

  if (!overdue.length) {
    bar.style.display = 'none';
    return;
  }

  bar.style.display = 'flex';
  bar.innerHTML = `
    ⚠️ <strong>${overdue.length} order${overdue.length > 1 ? 's' : ''} overdue (2+ hours old)</strong>
    — oldest: ${formatOrderAge(overdue[0].created_at)} •
    <button onclick="bulkSelectOverdue()" style="margin-left:4px;padding:2px 8px;font-size:11px;border-radius:4px;border:1px solid rgba(239,68,68,.4);background:transparent;color:#ef4444;cursor:pointer;">Select all overdue</button>
  `;
}

// ── Kanban patch — add age badges ─────────────────────────

// Patch the existing renderKanban to add age badges
(function() {
  if (typeof renderKanban !== 'function') return;
  const _orig = window.renderKanban;
  window.renderKanban = function(orders) {
    _orig(orders);
    // After rendering, inject age badges into each card
    const board = document.getElementById('kanban-board');
    if (!board) return;
    (orders || []).forEach(o => {
      const card = board.querySelector(`.kanban-card[data-order-id="${o.id}"]`);
      if (card) {
        const existingAge = card.querySelector('.kanban-card-age');
        if (!existingAge) {
          const ageSpan = document.createElement('span');
          ageSpan.className = `kanban-card-age ${orderAgeClass(o.created_at, o.status)}`;
          ageSpan.textContent = formatOrderAge(o.created_at);
          card.appendChild(ageSpan);
        }
      }
    });
    renderSlaAlert(orders);
  };
})();

// Patch renderKanban card rendering to add data-order-id attribute
(function() {
  if (typeof renderKanban !== 'function') return;
  // Also patch the kanban card HTML — add data-order-id via innerHTML patch
  const _orig2 = window.renderKanban;
  window.renderKanban = function(orders) {
    _orig2(orders);
    // Tag each card after render
    const board = document.getElementById('kanban-board');
    if (!board) return;
    board.querySelectorAll('.kanban-card').forEach((card, idx) => {
      const idEl = card.querySelector('.kanban-card-id');
      if (idEl && !card.dataset.orderId) {
        const id = parseInt(idEl.textContent.replace('#', ''), 10);
        if (!isNaN(id)) card.dataset.orderId = id;
      }
    });
  };
})();

// ── Bulk actions ──────────────────────────────────────────

let _selectedOrderIds = new Set();

// UI/UX audit: revives previously dead bulk-selection code (it existed but
// targeted DOM ids that never existed, so it never ran) — see updateBulkBar().
function toggleSelectAllOrders(checked) {
  const visibleIds = Array.from(document.querySelectorAll('.order-checkbox')).map(cb => parseInt(cb.dataset.id, 10));
  if (checked) visibleIds.forEach(id => _selectedOrderIds.add(id));
  else visibleIds.forEach(id => _selectedOrderIds.delete(id));
  document.querySelectorAll('.order-checkbox').forEach(cb => { cb.checked = checked; });
  updateBulkBar();
}

function toggleOrderSelect(orderId) {
  if (_selectedOrderIds.has(orderId)) {
    _selectedOrderIds.delete(orderId);
  } else {
    _selectedOrderIds.add(orderId);
  }
  updateBulkBar();
}

function updateBulkBar() {
  let bar = document.getElementById('bulk-actions-bar');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'bulk-actions-bar';
    bar.className = 'bulk-actions-bar';
    // UI/UX audit: button labels didn't match the status they actually set
    // (e.g. "✅ Preparing" set status to "confirmed", not "preparing") and
    // this bar was never actually appended anywhere — it targeted
    // #orders-section/[data-section="orders"], neither of which exist (the
    // real container is #section-orders) — so bulk actions silently never
    // worked. Fixed both.
    bar.innerHTML = `
      <span id="bulk-count">0 selected</span>
      <button class="bulk-btn green" onclick="bulkUpdateStatus('confirmed')">✅ Confirm</button>
      <button class="bulk-btn amber" onclick="bulkUpdateStatus('preparing')">🍳 Preparing</button>
      <button class="bulk-btn amber" onclick="bulkUpdateStatus('ready')">🎉 Ready</button>
      <button class="bulk-btn green" onclick="bulkUpdateStatus('delivered')">📦 Delivered</button>
      <button class="bulk-btn red"   onclick="bulkUpdateStatus('cancelled')">❌ Cancel</button>
      <button class="bulk-btn-cancel" onclick="clearBulkSelect()">✕ Clear</button>
    `;
    const ordersSection = document.getElementById('section-orders');
    if (ordersSection) ordersSection.insertAdjacentElement('afterbegin', bar);
  }

  if (_selectedOrderIds.size > 0) {
    bar.classList.add('visible');
    document.getElementById('bulk-count').textContent = `${_selectedOrderIds.size} selected`;
  } else {
    bar.classList.remove('visible');
  }
}

function clearBulkSelect() {
  _selectedOrderIds.clear();
  document.querySelectorAll('.order-checkbox').forEach(cb => { cb.checked = false; });
  updateBulkBar();
}

function bulkSelectOverdue() {
  (_ordersData || []).forEach(o => {
    const done = ['completed','delivered','cancelled','refunded'];
    if (!done.includes((o.status||'').toLowerCase()) && orderAgeMinutes(o.created_at) > 120) {
      _selectedOrderIds.add(o.id);
      const cb = document.querySelector(`.order-checkbox[data-id="${o.id}"]`);
      if (cb) cb.checked = true;
    }
  });
  updateBulkBar();
}

async function bulkUpdateStatus(newStatus) {
  if (!_selectedOrderIds.size) return;
  if (!confirm(`Update ${_selectedOrderIds.size} order(s) to "${newStatus}"?`)) return;

  const ids = [..._selectedOrderIds];
  let done = 0, failed = 0;

  for (const id of ids) {
    try {
      await apiFetch(`/orders/${id}/status`, {
        method:  'PUT',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ status: newStatus }),
      });
      done++;
    } catch (_) {
      failed++;
    }
  }

  toast(`Updated ${done} order(s)${failed ? ` (${failed} failed)` : ''}`);
  clearBulkSelect();
  loadOrders(); // refresh
}

// ── Advanced order filters ────────────────────────────────

let _orderFilters = { status: '', payment: '', search: '' };

function renderOrderFilters() {
  const section = document.getElementById('orders-section') || document.querySelector('[data-section="orders"]');
  if (!section || document.getElementById('order-filters-bar')) return;

  const bar = document.createElement('div');
  bar.id = 'order-filters-bar';
  bar.className = 'order-filters-bar';
  bar.innerHTML = `
    <select class="order-filter-select" id="filter-status" onchange="applyOrderFilters()" title="Filter by order status">
      <option value="">All Statuses</option>
      <option value="pending">Pending</option>
      <option value="confirmed">Confirmed</option>
      <option value="preparing">Preparing</option>
      <option value="ready">Ready</option>
      <option value="out_for_delivery">Out for Delivery</option>
      <option value="delivered">Delivered</option>
      <option value="completed">Completed</option>
      <option value="cancelled">Cancelled</option>
    </select>
    <select class="order-filter-select" id="filter-payment" onchange="applyOrderFilters()" title="Filter by payment status">
      <option value="">All Payments</option>
      <option value="pending">Payment Pending</option>
      <option value="awaiting_payment">Awaiting Payment</option>
      <option value="pending_cash">Cash Confirmed</option>
      <option value="paid">Paid</option>
      <option value="cancelled">Cancelled</option>
    </select>
    <input type="text" class="order-filter-select" id="filter-search"
           placeholder="Search phone or product…"
           oninput="applyOrderFilters()"
           style="min-width:160px;">
    <button class="bulk-btn" onclick="clearOrderFilters()" style="font-size:11px;padding:4px 10px;">✕ Clear</button>
  `;

  const firstCard = section.querySelector('.card, table');
  if (firstCard) firstCard.insertAdjacentElement('beforebegin', bar);
  else section.insertAdjacentElement('afterbegin', bar);
}

function applyOrderFilters() {
  _orderFilters.status  = document.getElementById('filter-status')?.value  || '';
  _orderFilters.payment = document.getElementById('filter-payment')?.value || '';
  _orderFilters.search  = (document.getElementById('filter-search')?.value || '').toLowerCase();

  let filtered = (_ordersData || []).filter(o => {
    if (_orderFilters.status  && o.status         !== _orderFilters.status)  return false;
    if (_orderFilters.payment && o.payment_status !== _orderFilters.payment) return false;
    if (_orderFilters.search) {
      const hay = `${o.customer_phone||''} ${o.product_name||''}`.toLowerCase();
      if (!hay.includes(_orderFilters.search)) return false;
    }
    return true;
  });

  const tbody = document.getElementById('orders-body');
  if (!tbody) return;

  // Reuse existing renderOrders but with filtered data
  if (typeof renderOrders === 'function') {
    renderOrders(filtered, 'orders-body', true);
  }
}

function clearOrderFilters() {
  _orderFilters = { status: '', payment: '', search: '' };
  const s = document.getElementById('filter-status');
  const p = document.getElementById('filter-payment');
  const q = document.getElementById('filter-search');
  if (s) s.value = '';
  if (p) p.value = '';
  if (q) q.value = '';
  if (typeof renderOrders === 'function' && _ordersData) {
    renderOrders(_ordersData, 'orders-body', true);
  }
}

// ── Wire into existing loadOrders ─────────────────────────
(function() {
  const _origLoad = window.loadOrders;
  if (typeof _origLoad !== 'function') return;
  window.loadOrders = async function() {
    await _origLoad.apply(this, arguments);
    // renderOrderFilters() removed from here — it never actually attached
    // to the page (see comment above filterOrdersByPayment()) and its one
    // genuinely useful capability, the payment-status filter, now lives in
    // the real Orders toolbar instead.
    if (_ordersData) renderSlaAlert(_ordersData);
  };
})();


/* ══════════════════════════════════════════════════════════
   PHASE 5 — GROWTH INSIGHTS CARD (dashboard)
   Loads from GET /insights/growth and injects a card.
   Additive only — placed in overview section.
══════════════════════════════════════════════════════════ */

async function loadGrowthInsights() {
  try {
    const data = await apiFetch('/insights/growth');
    renderGrowthCard(data);
  } catch (e) {
    console.warn('Growth insights not available:', e.message);
  }
}

function renderGrowthCard(data) {
  const wins = data.quick_wins || [];
  if (!wins.length) return;

  const existingCard = document.getElementById('growth-insights-card');
  if (existingCard) {
    existingCard.remove();
  }

  const card = document.createElement('div');
  card.id = 'growth-insights-card';
  card.className = 'card';
  card.style.cssText = 'margin-bottom:16px;';

  const rows = wins.map(w => `
    <div style="display:flex;align-items:flex-start;gap:10px;padding:10px 0;border-bottom:1px solid var(--border,#2a3830);">
      <span style="font-size:18px;flex-shrink:0;">${w.priority === 'high' ? '🔴' : '🟡'}</span>
      <div style="flex:1;min-width:0;">
        <div style="font-size:13px;font-weight:600;color:var(--text,#e8f5e9);">${escHtml(w.title)}</div>
        <div style="font-size:11px;color:var(--text-dim,#6b8f71);margin-top:2px;">${escHtml(w.value)}</div>
      </div>
      <button onclick="window.open('${w.endpoint}','_blank')" style="padding:4px 10px;font-size:11px;border-radius:6px;border:1px solid var(--border,#2a3830);background:transparent;color:var(--green,#22c55e);cursor:pointer;white-space:nowrap;flex-shrink:0;">${escHtml(w.action)}</button>
    </div>
  `).join('');

  card.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
      <span style="font-size:16px;">💡</span>
      <strong style="font-size:14px;">Growth Opportunities</strong>
      <span style="font-size:11px;color:var(--text-dim,#6b8f71);margin-left:auto;">${wins.length} action${wins.length > 1 ? 's' : ''}</span>
    </div>
    ${rows}
  `;

  // Insert at top of overview section
  const overview = document.getElementById('overview-section') || document.querySelector('[data-section="overview"]');
  if (overview) {
    const firstCard = overview.querySelector('.card');
    if (firstCard) firstCard.insertAdjacentElement('beforebegin', card);
    else overview.insertAdjacentElement('afterbegin', card);
  }
}

// Auto-load growth insights when dashboard overview tab is shown
(function() {
  const _origSwitch = window.switchTab || window.showSection;
  if (typeof _origSwitch !== 'function') return;
  const fnName = window.switchTab ? 'switchTab' : 'showSection';
  const _orig  = window[fnName];
  window[fnName] = function(name, ...args) {
    _orig.call(this, name, ...args);
    if (name === 'overview' || name === 'dashboard') {
      loadGrowthInsights();
    }
  };
})();

// Load growth insights only after login — single consolidated call
// (all post-login async loads are batched here to prevent a request storm)
setTimeout(() => {
  if (typeof token !== 'undefined' && token) {
    loadGrowthInsights();
  }
}, 2500);


/* ══════════════════════════════════════════════════════════
   UX ENHANCEMENTS — Dashboard Phases 3-6
   All additive. Existing functions unchanged.
══════════════════════════════════════════════════════════ */

/* ── Phase 3: Onboarding Wizard ── */

let _wizardDismissed = localStorage.getItem('wazi_wizard_dismissed') === '1';

async function loadOnboardingWizard() {
  if (_wizardDismissed) return;
  try {
    const data = await apiFetch('/onboarding/status');
    if (!data.show_wizard) return;
    renderOnboardingWizard(data);
  } catch (_) {}
}

function renderOnboardingWizard(data) {
  const section = document.getElementById('section-overview');
  if (!section || document.getElementById('wizard-card')) return;

  const steps = data.steps || {};
  const dots  = [1,2,3,4,5].map(i => {
    const done   = steps[i];
    const active = !done && i === data.next_step;
    const cls    = done ? 'done' : active ? 'active' : 'todo';
    return `<div class="wizard-step-dot ${cls}" title="Step ${i}">${done ? '✓' : i}</div>`;
  }).join('');

  const tip  = data.next_tip || {};
  const card = document.createElement('div');
  card.id = 'wizard-card';
  card.className = 'wizard-card';
  card.innerHTML = `
    <div class="wizard-header">
      <span class="wizard-title">🚀 Setup Guide — ${data.completed}/${data.total} complete</span>
      <button class="wizard-dismiss" onclick="dismissWizard()">✕ Dismiss</button>
    </div>
    <div class="wizard-progress">${dots}</div>
    ${tip.title ? `
    <div class="wizard-next">
      <span class="wizard-next-icon">${tip.icon || '📋'}</span>
      <div style="flex:1;">
        <div class="wizard-next-title">Step ${tip.step}: ${escHtml(tip.title)}</div>
        <div class="wizard-next-desc">${escHtml(tip.description || '')}</div>
      </div>
      <button class="wizard-action" onclick="showSection('${tip.action_section || 'overview'}', null)">${escHtml(tip.action || 'Go →')}</button>
    </div>` : ''}
  `;

  const firstCard = section.querySelector('.card, .stats-grid, .stat-row');
  if (firstCard) firstCard.insertAdjacentElement('beforebegin', card);
  else section.insertAdjacentElement('afterbegin', card);
}

function dismissWizard() {
  _wizardDismissed = true;
  localStorage.setItem('wazi_wizard_dismissed', '1');
  document.getElementById('wizard-card')?.remove();
}

// Load wizard after overview loads
(function() {
  const _orig = window.showSection;
  window.showSection = function(name, ...args) {
    _orig.call(this, name, ...args);
    if (name === 'overview') setTimeout(loadOnboardingWizard, 800);
  };
})();
setTimeout(() => { if (!_wizardDismissed && typeof token !== 'undefined' && token) loadOnboardingWizard(); }, 3000);


/* ── Phase 4: Health Center ── */

async function loadHealthStatus() {
  const section = document.getElementById('section-overview');
  if (!section) return;

  try {
    const data   = await apiFetch('/health/status');
    const checks = data.checks || {};

    let existing = document.getElementById('health-center-card');
    if (!existing) {
      existing = document.createElement('div');
      existing.id = 'health-center-card';
      existing.className = 'card';
      existing.style.marginBottom = '16px';
      const lastCard = section.querySelector('.card:last-of-type');
      if (lastCard) lastCard.insertAdjacentElement('afterend', existing);
      else section.appendChild(existing);
    }

    const overallColor = data.overall === 'green' ? '#22c55e' : data.overall === 'yellow' ? '#f59e0b' : '#ef4444';
    const overallLabel = data.overall === 'green' ? 'All systems operational' : data.overall === 'yellow' ? 'Some warnings' : 'Issues detected';

    const items = Object.entries(checks).map(([key, v]) => `
      <div class="health-item">
        <div class="health-dot ${v.status}"></div>
        <div>
          <div class="health-item-label">${escHtml(key.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase()))}</div>
          <div class="health-item-msg">${escHtml(v.message || '')}</div>
        </div>
      </div>
    `).join('');

    existing.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;">
        <span style="font-size:16px;">🩺</span>
        <strong style="font-size:14px;">System Health</strong>
        <span style="margin-left:auto;font-size:11px;color:${overallColor};font-weight:600;">${overallLabel}</span>
      </div>
      <div class="health-grid">${items}</div>
    `;
  } catch (_) {}
}


/* ── Phase 6: Customer Success Nudges ── */

async function loadSuccessNudges() {
  const section = document.getElementById('section-overview');
  if (!section || document.getElementById('nudge-container')) return;

  try {
    const data = await apiFetch('/insights/growth');
    const wins = (data.quick_wins || []).slice(0, 3);
    if (!wins.length) return;

    const container = document.createElement('div');
    container.id = 'nudge-container';
    container.style.marginBottom = '12px';

    container.innerHTML = wins.map(w => `
      <div class="nudge-bar" onclick="window.open('${w.endpoint}','_blank')">
        <span class="nudge-bar-icon">${w.priority === 'high' ? '🔴' : '🟡'}</span>
        <div class="nudge-bar-text">${escHtml(w.title)} — <em>${escHtml(w.value)}</em></div>
        <span class="nudge-bar-action">${escHtml(w.action)} →</span>
      </div>
    `).join('');

    const firstCard = section.querySelector('.card, .stats-grid');
    if (firstCard) firstCard.insertAdjacentElement('beforebegin', container);
  } catch (_) {}
}


/* ── Help panel & Command palette (dashboard) ── */

// Inject help FAB — only after login (called from init() instead of on DOMContentLoaded)
// The IIFE below is intentionally removed to prevent FAB showing on login screen.
// injectDashboardHelp() is called inside init() which only runs post-login.

function injectDashboardHelp() {
  if (document.getElementById('dash-help-fab')) return;

  // FAB
  const fab = document.createElement('button');
  fab.id = 'dash-help-fab';
  fab.className = 'help-fab';
  fab.title = 'Ask WaziBot for help';
  fab.textContent = '?';
  fab.onclick = toggleDashHelp;
  document.body.appendChild(fab);

  // Panel
  const panel = document.createElement('div');
  panel.id = 'dash-help-panel';
  panel.className = 'help-panel';
  panel.innerHTML = `
    <div class="help-panel-header">
      💬 Ask WaziBot
      <button class="help-panel-close" onclick="closeDashHelp()" aria-label="Close help panel" title="Close">✕</button>
    </div>
    <div class="help-panel-input-row">
      <input class="help-panel-input" id="dash-help-input" placeholder="How do I…?" aria-label="Ask WaziBot a question" onkeydown="if(event.key==='Enter')askDashHelp()">
      <button class="help-panel-send" onclick="askDashHelp()" aria-label="Send question" title="Send">→</button>
    </div>
    <div class="help-panel-body" id="dash-help-body">
      <div class="help-quick-links">
        <button class="help-quick-link" onclick="askDashHelpQ('How do I send a campaign?')">Campaigns</button>
        <button class="help-quick-link" onclick="askDashHelpQ('How do I add products?')">Products</button>
        <button class="help-quick-link" onclick="askDashHelpQ('How do payment reminders work?')">Reminders</button>
        <button class="help-quick-link" onclick="askDashHelpQ('How do bookings work?')">Bookings</button>
        <button class="help-quick-link" onclick="askDashHelpQ('How do referrals work?')">Referrals</button>
      </div>
      <div style="color:var(--text-dim,#6b8f71);font-size:12px;">Ask anything about using WaziBot. 😊</div>
    </div>
  `;
  document.body.appendChild(panel);

  // Command palette
  const cmdOverlay = document.createElement('div');
  cmdOverlay.id = 'dash-cmd-overlay';
  cmdOverlay.className = 'cmd-overlay';
  cmdOverlay.onclick = e => { if (e.target === cmdOverlay) closeDashCmd(); };
  cmdOverlay.innerHTML = `
    <div class="cmd-box">
      <div class="cmd-input-row">
        <span class="cmd-icon">⌘</span>
        <input class="cmd-input" id="dash-cmd-input" placeholder="Search or type a command…"
               oninput="renderDashCmdResults()" onkeydown="dashCmdKeyDown(event)" autocomplete="off">
      </div>
      <div class="cmd-results" id="dash-cmd-results"></div>
      <div class="cmd-footer"><span><kbd>↑↓</kbd> Navigate</span><span><kbd>Enter</kbd> Select</span><span><kbd>Esc</kbd> Close</span></div>
    </div>
  `;
  document.body.appendChild(cmdOverlay);

  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); openDashCmd(); }
    if (e.key === 'Escape') { closeDashHelp(); closeDashCmd(); }
  });
}

let _dashHelpOpen = false;
function toggleDashHelp() { _dashHelpOpen = !_dashHelpOpen; document.getElementById('dash-help-panel')?.classList.toggle('open', _dashHelpOpen); if (_dashHelpOpen) setTimeout(() => document.getElementById('dash-help-input')?.focus(), 50); }
function closeDashHelp()  { _dashHelpOpen = false; document.getElementById('dash-help-panel')?.classList.remove('open'); }
function askDashHelpQ(q)  { const inp = document.getElementById('dash-help-input'); if (inp) inp.value = q; askDashHelp(); }

async function askDashHelp() {
  const q = (document.getElementById('dash-help-input')?.value || '').trim();
  if (!q) return;
  const body = document.getElementById('dash-help-body');
  if (!body) return;
  body.innerHTML = '<div style="color:var(--text-dim);font-size:12px;">Looking up…</div>';
  try {
    const data = await apiFetch('/support/ask', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, context: 'dashboard' }),
    });
    let html = `<div class="help-answer">${(data.answer||'').replace(/\*(.*?)\*/g,'<strong>$1</strong>')}</div>`;
    if (data.steps?.length) html += `<ol class="help-steps">${data.steps.map(s=>`<li>${s}</li>`).join('')}</ol>`;
    if (data.tips?.length)  html += data.tips.map(t=>`<div class="help-tip">💡 ${t}</div>`).join('');
    if (data.related?.length) html += `<div class="help-related">${data.related.map(r=>`<button class="help-related-chip" onclick="askDashHelpQ('Tell me about ${r.title}')">${r.title}</button>`).join('')}</div>`;
    body.innerHTML = html;
  } catch (e) {
    body.innerHTML = `<div style="color:var(--red);font-size:12px;">Error: ${e.message}</div>`;
  }
}

const DASH_COMMANDS = [
  { icon: '📊', label: 'Overview',          action: () => showSection('overview') },
  { icon: '🛒', label: 'Orders',            action: () => showSection('orders') },
  { icon: '📋', label: 'Products',          action: () => showSection('inventory') },
  { icon: '👤', label: 'Customers',         action: () => showSection('customers') },
  { icon: '📣', label: 'Campaigns',         action: () => showSection('campaigns') },
  { icon: '⚙️', label: 'Settings',          action: () => showSection('settings') },
  { icon: '💬', label: 'Open Inbox',        action: () => window.location.href = '/inbox' },
  { icon: '⭐', label: 'VIP Customers',     action: () => showSection('customers') },
  { icon: '💳', label: 'Payment Settings',  action: () => showSection('settings') },
  { icon: '🩺', label: 'System Health',     action: () => { showSection('overview'); setTimeout(loadHealthStatus,300); } },
  { icon: '❓', label: 'Help / Ask WaziBot', action: () => { closeDashCmd(); toggleDashHelp(); } },
];
let _dashCmdActive = 0;
let _dashCmdFiltered = DASH_COMMANDS;

function openDashCmd() {
  _dashCmdActive = 0;
  _dashCmdFiltered = DASH_COMMANDS;
  document.getElementById('dash-cmd-overlay')?.classList.add('open');
  const inp = document.getElementById('dash-cmd-input');
  if (inp) { inp.value = ''; inp.focus(); }
  renderDashCmdResults();
}
function closeDashCmd() { document.getElementById('dash-cmd-overlay')?.classList.remove('open'); }

function renderDashCmdResults() {
  const q = (document.getElementById('dash-cmd-input')?.value || '').toLowerCase();
  _dashCmdFiltered = q ? DASH_COMMANDS.filter(c => c.label.toLowerCase().includes(q)) : DASH_COMMANDS;
  const box = document.getElementById('dash-cmd-results');
  if (!box) return;
  box.innerHTML = _dashCmdFiltered.map((c, i) => `
    <div class="cmd-result ${i === _dashCmdActive ? 'active' : ''}" onclick="execDashCmd(${i})">
      <span class="cmd-result-icon">${c.icon}</span>
      <div class="cmd-result-text">${c.label}</div>
    </div>`).join('');
}

function execDashCmd(i) { const c = _dashCmdFiltered[i]; if (c) { closeDashCmd(); c.action(); } }
function dashCmdKeyDown(e) {
  if (e.key === 'Escape')    { closeDashCmd(); return; }
  if (e.key === 'ArrowDown') { _dashCmdActive = Math.min(_dashCmdActive+1, _dashCmdFiltered.length-1); renderDashCmdResults(); e.preventDefault(); return; }
  if (e.key === 'ArrowUp')   { _dashCmdActive = Math.max(_dashCmdActive-1, 0); renderDashCmdResults(); e.preventDefault(); return; }
  if (e.key === 'Enter')     { execDashCmd(_dashCmdActive); e.preventDefault(); }
}

// Auto-load health + nudges on overview
(function() {
  const _orig = window.showSection;
  window.showSection = function(name, ...args) {
    _orig.call(this, name, ...args);
    if (name === 'overview') {
      setTimeout(loadHealthStatus, 1000);
      // loadSuccessNudges removed from auto-start (Sprint 10: optional only)
    }
  };
})();
setTimeout(() => { if (typeof token !== 'undefined' && token) { loadHealthStatus(); } }, 4000); // loadSuccessNudges: optional


/* ══════════════════════════════════════════════════════════
   REFERRALS TAB — Settings → Referrals
   All additive. Calls /me/referral and /marketing/referral-message
══════════════════════════════════════════════════════════ */

let _refData = null;
let _refMsgType = 'whatsapp';

async function loadReferralTab() {
  if (_refData) { renderReferralTab(_refData); return; }
  try {
    _refData = await apiFetch('/me/referral');
    renderReferralTab(_refData);
    await loadReferralMessage('whatsapp');
    loadReferralQR();  // fire-and-forget — the tab's other content doesn't wait on this
  } catch (e) {
    console.warn('Referral load failed:', e.message);
  }
}

async function loadReferralQR() {
  const img     = document.getElementById('ref-qr-img');
  const loading = document.getElementById('ref-qr-loading');
  const errEl   = document.getElementById('ref-qr-error');
  const dlBtn   = document.getElementById('ref-qr-download');
  if (!img) return;
  try {
    const token = localStorage.getItem('wazi_token') || '';
    const resp  = await fetch('/me/referral/qr', { headers: { 'Authorization': `Bearer ${token}` } });
    if (!resp.ok) throw new Error(await resp.text());
    const blob = await resp.blob();
    const url  = URL.createObjectURL(blob);
    img.src = url;
    img.style.display = 'block';
    if (loading) loading.style.display = 'none';
    if (errEl)   errEl.style.display   = 'none';
    if (dlBtn) {
      dlBtn.href = url;
      const code = (_refData && _refData.referral_code) || 'wazibot';
      dlBtn.download = `wazibot-referral-${code}.png`;
      dlBtn.style.display = 'inline-flex';
    }
  } catch (e) {
    if (loading) loading.style.display = 'none';
    if (errEl) { errEl.textContent = 'Could not generate QR: ' + e.message; errEl.style.display = 'block'; }
  }
}

function renderReferralTab(data) {
  const code      = data.referral_code      || '—';
  const link      = data.referral_link      || '—';
  const total     = data.total_referrals    ?? '0';
  const conv      = data.converted          ?? '0';
  const available = parseFloat(data.available_balance  ?? data.pending_reward ?? 0);
  const totalEarned = available + parseFloat(data.total_withdrawn ?? 0) + parseFloat(data.pending_balance ?? 0);
  const MIN_WITHDRAW = 5.00;

  const _el = (id, val) => { const e = document.getElementById(id); if(e) e.textContent = val; };

  _el('ref-code-display',    code);
  _el('ref-link-display',    link);
  _el('ref-stat-total',      total);
  _el('ref-stat-converted',  conv);
  _el('ref-stat-available',  `$${available.toFixed(2)}`);
  _el('ref-stat-pending',    `$${totalEarned.toFixed(2)}`);
  _el('ref-withdraw-available', `$${available.toFixed(2)}`);

  // Progress bar toward $5.00 minimum withdrawal
  const pct = Math.min(100, (available / MIN_WITHDRAW) * 100);
  const bar = document.getElementById('ref-progress-bar');
  const lbl = document.getElementById('ref-progress-label');
  const needed = document.getElementById('ref-refs-needed');
  if (bar) bar.style.width = pct + '%';
  if (lbl) lbl.textContent = `$${available.toFixed(2)} / $${MIN_WITHDRAW.toFixed(2)}`;
  if (needed) {
    const refsLeft = Math.max(0, Math.ceil((MIN_WITHDRAW - available) / 0.20));
    needed.textContent = refsLeft > 0 ? `${refsLeft} more referral${refsLeft !== 1 ? 's' : ''} to unlock withdrawal` : '✅ Ready to withdraw!';
    needed.style.color = refsLeft === 0 ? 'var(--green)' : 'var(--text-dim)';
  }

  // Show/hide withdrawal panel based on available balance
  const withdrawPanel = document.getElementById('ref-withdraw-panel');
  if (withdrawPanel) withdrawPanel.style.display = available >= MIN_WITHDRAW ? 'block' : 'none';

  // Pre-fill withdrawal amount with available balance (capped to available)
  const amtInput = document.getElementById('ref-withdraw-amount');
  if (amtInput && available >= MIN_WITHDRAW) amtInput.value = available.toFixed(2);

  // Nav badge
  const badge = document.getElementById('nav-ref-badge');
  if (badge && parseInt(total) > 0) {
    badge.textContent   = total;
    badge.style.display = 'inline-flex';
  }
}

async function submitReferralWithdrawal() {
  const email  = (document.getElementById('ref-paypal-email')?.value  || '').trim();
  const amount = parseFloat(document.getElementById('ref-withdraw-amount')?.value || '0');
  const btn    = document.getElementById('ref-withdraw-btn');
  const status = document.getElementById('ref-withdraw-status');

  if (!email || !email.includes('@')) { toast('Enter a valid PayPal email', true); return; }
  if (amount < 5.00) { toast('Minimum withdrawal is $5.00', true); return; }
  if (!confirm(`Request payout of $${amount.toFixed(2)} to ${email}?`)) return;

  try {
    if (btn) { btn.disabled = true; btn.textContent = 'Submitting…'; }
    const result = await apiFetch('/me/referral/withdraw', {
      method: 'POST',
      body:   JSON.stringify({ paypal_email: email, amount }),
    });
    if (status) status.innerHTML = `<div style="color:var(--green);line-height:1.6;">✅ ${result.message}</div>`;
    toast('✅ Payout requested!');
    // Refresh stats
    _refData = null;
    setTimeout(loadReferralTab, 1000);
  } catch(e) {
    if (status) status.innerHTML = `<span style="color:#ff5252">❌ ${e.message}</span>`;
    toast(e.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '💸 Request Payout'; }
  }
}

async function loadReferralMessage(type = 'whatsapp') {
  _refMsgType = type;
  const preview = document.getElementById('ref-message-preview');
  if (!preview) return;
  preview.value = 'Loading…';

  try {
    if (type === 'whatsapp') {
      const data = await apiFetch('/marketing/referral-message');
      preview.value = data.message || '';
    } else {
      const data = await apiFetch('/marketing/copy?business_type=general&tone=friendly');
      const fb   = data.facebook_awareness;
      preview.value = (fb?.caption || '') + (fb?.hashtags ? '\n\n' + fb.hashtags : '');
    }
  } catch (e) {
    preview.value = 'Could not load message. Please try again.';
  }
}

function copyRef(type) {
  if (!_refData) return;
  const text = type === 'code' ? _refData.referral_code : _refData.referral_link;
  if (!text || text === '—') return;
  navigator.clipboard.writeText(text).then(() => {
    toast(type === 'code' ? 'Referral code copied! ✓' : 'Referral link copied! ✓');
  }).catch(() => {
    // Fallback
    const el = document.getElementById(type === 'code' ? 'ref-code-display' : 'ref-link-display');
    if (el) { const range = document.createRange(); range.selectNode(el); window.getSelection().removeAllRanges(); window.getSelection().addRange(range); document.execCommand('copy'); }
    toast('Copied! ✓');
  });
}

function copyRefMessage() {
  const preview = document.getElementById('ref-message-preview');
  if (!preview || !preview.value) return;
  navigator.clipboard.writeText(preview.value).then(() => {
    toast('Message copied! ✓');
  }).catch(() => {
    preview.select();
    document.execCommand('copy');
    toast('Message copied! ✓');
  });
}

function shareRefWhatsApp() {
  const preview = document.getElementById('ref-message-preview');
  const text    = preview?.value || (_refData?.referral_link || '');
  if (!text) return;
  const encoded = encodeURIComponent(text.substring(0, 1000));
  window.open(`https://wa.me/?text=${encoded}`, '_blank');
}

// Auto-load referral data when user opens the Referrals nav item directly
// (already wired via onclick in the nav button above)

// Also show a subtle referral nudge on Overview if they have no referrals yet
async function maybeShowReferralNudge() {
  try {
    const data = await apiFetch('/me/referral');
    if (parseInt(data.total_referrals || 0) === 0) {
      const section = document.getElementById('section-overview');
      if (!section || document.getElementById('ref-nudge')) return;
      const nudge = document.createElement('div');
      nudge.id = 'ref-nudge';
      nudge.style.cssText = 'display:flex;align-items:center;gap:10px;padding:10px 14px;background:rgba(34,197,94,.06);border:1px solid rgba(34,197,94,.15);border-radius:8px;margin-bottom:12px;font-size:12px;color:var(--text-dim);cursor:pointer;';
      nudge.onclick = () => { showSection('settings', null); switchSettingsTab('referrals', null); loadReferralTab(); };
      nudge.innerHTML = `
        <span style="font-size:16px;">🔗</span>
        <div style="flex:1;">Earn rewards by referring other businesses to WaziBot.</div>
        <span style="color:var(--green);font-size:11px;white-space:nowrap;">Get my link →</span>
      `;
      const firstCard = section.querySelector('.card, .stats-grid, .stat-row');
      if (firstCard) firstCard.insertAdjacentElement('afterend', nudge);
    }
  } catch (_) {}
}

// Load referral nudge a few seconds after the page settles
setTimeout(() => { if (typeof token !== 'undefined' && token) maybeShowReferralNudge(); }, 5000);


/* ══════════════════════════════════════════════════════════
   PRODUCTS PAGE — New functions for Phases 6-11 + 13
   All additive. Existing deleteProduct unchanged.
══════════════════════════════════════════════════════════ */

// ── Phase 9: Bulk select ──────────────────────────────────

function toggleProductSelect(id, checked) {
  if (checked) _selectedProductIds.add(id);
  else _selectedProductIds.delete(id);
  _updateBulkBar();
}

function toggleSelectAllProducts(checked) {
  const visible = _allProducts.filter(() => true); // filtered subset
  visible.forEach(p => checked ? _selectedProductIds.add(p.id) : _selectedProductIds.delete(p.id));
  document.querySelectorAll('.prod-cb').forEach(cb => { cb.checked = checked; });
  _updateBulkBar();
}

function clearProductSelection() {
  _selectedProductIds.clear();
  const allCb = document.getElementById('prod-select-all');
  if (allCb) allCb.checked = false;
  document.querySelectorAll('.prod-cb').forEach(cb => { cb.checked = false; });
  _updateBulkBar();
}

function _updateBulkBar() {
  const bar   = document.getElementById('prod-bulk-bar');
  const count = document.getElementById('prod-bulk-count');
  if (!bar) return;
  const n = _selectedProductIds.size;
  bar.style.display  = n > 0 ? 'flex' : 'none';
  if (count) count.textContent = `${n} selected`;
}

function bulkProductAction(action) {
  const n = _selectedProductIds.size;
  if (!n) return;
  const modal    = document.getElementById('prod-bulk-modal');
  const titleEl  = document.getElementById('bulk-modal-title');
  const msgEl    = document.getElementById('bulk-modal-msg');
  const confirmBtn = document.getElementById('bulk-modal-confirm');
  if (!modal) return;

  const labels = { delete: 'Delete', activate: 'Activate', deactivate: 'Deactivate' };
  if (titleEl)  titleEl.textContent = `${labels[action]} ${n} Product${n > 1 ? 's' : ''}`;
  if (msgEl) {
    if (action === 'delete')
      msgEl.textContent = `Are you sure you want to permanently delete ${n} product${n>1?'s':''}? This cannot be undone.`;
    else
      msgEl.textContent = `${labels[action]} ${n} selected product${n>1?'s':''}?`;
  }
  if (confirmBtn) confirmBtn.onclick = () => _executeBulkAction(action);
  modal.style.display = 'flex';
}

async function _executeBulkAction(action) {
  closeBulkModal();
  const ids = [..._selectedProductIds];
  let done = 0, failed = 0;
  for (const id of ids) {
    try {
      if (action === 'delete') {
        await apiFetch(`${ROUTES.products}/${id}`, { method: 'DELETE' });
      } else {
        const status = action === 'activate' ? 'active' : 'draft';
        await apiFetch(`${ROUTES.products}/${id}`, {
          method: 'PATCH',
          body: JSON.stringify({ status }),
        });
      }
      done++;
    } catch (_) { failed++; }
  }
  toast(`${action === 'delete' ? '🗑' : '✅'} ${done} product${done > 1 ? 's' : ''} ${action}d${failed ? ` (${failed} failed)` : ''}`);
  clearProductSelection();
  loadProducts();
}

function closeBulkModal() {
  const modal = document.getElementById('prod-bulk-modal');
  if (modal) modal.style.display = 'none';
}

// ── Phase 6: Edit product modal ───────────────────────────

function openProdEdit(id) {
  const p = _allProducts.find(x => x.id === id);
  if (!p) { toast('Product not found', true); return; }
  document.getElementById('edit-prod-id').value    = p.id;
  document.getElementById('edit-prod-name').value  = p.name  || '';
  document.getElementById('edit-prod-price').value = p.price || '';
  document.getElementById('edit-prod-stock').value = typeof p.stock === 'number' ? p.stock : '';
  const lowStockEl = document.getElementById('edit-prod-low-stock-threshold');
  if (lowStockEl) lowStockEl.value = (typeof p.low_stock_threshold === 'number') ? p.low_stock_threshold : '';
  document.getElementById('edit-prod-desc').value  = p.description || '';
  const statusEl = document.getElementById('edit-prod-status');
  if (statusEl) statusEl.value = p.status || 'active';
  // UI/UX audit: the edit modal had no category field at all, so a
  // product's category could only ever be set once, at creation, with no
  // way to fix or change it afterward. Cloning the Add form's category
  // list (rather than duplicating ~240 lines of options here) keeps both
  // selects in sync automatically if that list ever changes.
  const catEl = document.getElementById('edit-prod-category');
  const sourceCat = document.getElementById('product-category');
  if (catEl && sourceCat && catEl.dataset.populated !== '1') {
    catEl.innerHTML = sourceCat.innerHTML;
    catEl.dataset.populated = '1';
  }
  if (catEl) catEl.value = p.category || '';
  _applyServiceMode(!!window.IS_SERVICE_BUSINESS);
  const modal = document.getElementById('prod-edit-modal');
  if (modal) modal.style.display = 'flex';
}

function closeProdEdit() {
  const modal = document.getElementById('prod-edit-modal');
  if (modal) modal.style.display = 'none';
}

async function saveProdEdit() {
  const id    = parseInt(document.getElementById('edit-prod-id').value, 10);
  const name  = document.getElementById('edit-prod-name').value.trim();
  const price = parseFloat(document.getElementById('edit-prod-price').value);
  const stock = document.getElementById('edit-prod-stock').value;
  const desc  = document.getElementById('edit-prod-desc').value.trim();
  const status= document.getElementById('edit-prod-status').value;
  if (!name || isNaN(price)) { toast('Name and price are required', true); return; }
  const payload = { name, price, status };
  if (desc)           payload.description = desc;
  if (stock !== '')   payload.stock = parseInt(stock, 10);
  const lowStockEl = document.getElementById('edit-prod-low-stock-threshold');
  const lowStockVal = lowStockEl ? lowStockEl.value : '';
  if (lowStockVal !== '') payload.low_stock_threshold = parseInt(lowStockVal, 10);
  const catEl = document.getElementById('edit-prod-category');
  if (catEl) payload.category = catEl.value || null;
  // If a new image was selected for editing, upload it first
  if (typeof _pendingEditImgDataUrl !== 'undefined' && _pendingEditImgDataUrl) {
    const editImgUrl = await uploadImageToSupabase(
      _pendingEditImgDataUrl,
      name.toLowerCase().replace(/[^a-z0-9]/g, '_') + '_edit_' + Date.now()
    );
    payload.image_url = editImgUrl;
    _pendingEditImgDataUrl = null;
  }
  try {
    await apiFetch(`${ROUTES.products}/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
    toast('✅ Product updated');
    closeProdEdit();
    loadProducts();
  } catch(e) { toast('Failed to update: ' + e.message, true); }
}

function viewProduct(id) {
  const p = _allProducts.find(x => x.id === id);
  if (!p) return;
  const info = [
    `📦 ${p.name}`,
    `💲 Price: ${getCurrencySymbol()}${(p.price||0).toFixed(2)}`,
    p.category   ? `🏷 Category: ${p.category}` : '',
    typeof p.stock === 'number' ? `📊 Stock: ${p.stock}` : '',
    p.description ? `📝 ${p.description}` : '',
  ].filter(Boolean).join('\n');
  alert(info); // simple modal-less view for now
}

// UI/UX audit (Phase 9): Products had no link to the orders containing them
// at all. Reuses the Orders search box (same pattern as viewOrdersForDrawer/
// viewOrdersFromChat) — matches by product name since orders store
// product_name rather than a product_id foreign key, same join key the
// search box itself already uses (applyOrderListFilters).
function viewOrdersForProduct(id) {
  const p = _allProducts.find(x => x.id === id);
  if (!p) return;
  showSection('orders', null);
  setTimeout(() => {
    const search = document.getElementById('order-search');
    if (search) { search.value = p.name; filterOrdersBySearch(p.name); }
  }, 300);
}

// UI/UX audit — Phase 9 recommendation ("Products -> Customers: who bought
// this?"), originally flagged as needing a new backend query and left for a
// follow-up. Calls the new read-only GET /products/{id}/customers endpoint,
// added specifically for this, and lists results in a small modal reusing
// the existing .prod-modal-* CSS (same component the Edit Product and Bulk
// Confirm modals already use) rather than a fourth modal styling system.
// Each row links to that customer's CRM drawer via the existing lookup
// pattern (viewCustomerFromChat/viewCrmForHandoffRow use the same shape).
async function viewCustomersForProduct(id) {
  const p = _allProducts.find(x => x.id === id);
  if (!p) return;
  const modal = document.getElementById('prod-customers-modal');
  const title = document.getElementById('prod-customers-modal-title');
  const body  = document.getElementById('prod-customers-modal-body');
  if (!modal || !body) return;
  title.textContent = `Customers — ${p.name}`;
  body.innerHTML = '<div style="color:var(--text-dim);font-family:var(--mono);font-size:11px;padding:8px 0;">Loading…</div>';
  modal.style.display = 'flex';

  try {
    const res = await apiFetch(`/products/${id}/customers`);
    const rows = (res && res.customers) || [];
    if (!rows.length) {
      body.innerHTML = '<div class="empty">No orders for this product yet.</div>';
      return;
    }
    body.innerHTML = rows.map(c => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border);">
        <div>
          <div style="font-size:13px;font-weight:600;">${escHtml(c.customer_name || c.phone)}</div>
          <div style="font-family:var(--mono);font-size:11px;color:var(--text-dim);">${escHtml(c.phone)} · ${c.order_count} order${c.order_count!==1?'s':''} · ${getCurrencySymbol()}${parseFloat(c.total_spent||0).toFixed(2)}</div>
        </div>
        <button class="btn btn-ghost" style="font-size:11px;padding:3px 8px;" onclick="viewCrmForHandoffRow('${escHtml(c.phone).replace(/'/g,"\\'")}');closeProductCustomersModal();" title="View CRM record">👤 CRM</button>
      </div>`).join('');
  } catch (e) {
    // Same visual pattern as _errorBlock(), but that helper calls its retry
    // function by bare name (no args) — this retry needs the product id, so
    // it's inlined here rather than stretching _errorBlock's contract.
    body.innerHTML = `<div class="empty" style="color:var(--red);">
      ⚠ Couldn't load customers for this product. Check your connection and try again.
      <div style="margin-top:8px;"><button class="btn btn-ghost" style="font-size:11px;padding:5px 12px;" onclick="viewCustomersForProduct(${id})">↻ Retry</button></div>
    </div>`;
  }
}

function closeProductCustomersModal() {
  const modal = document.getElementById('prod-customers-modal');
  if (modal) modal.style.display = 'none';
}

// ── Phase 10: Quick actions ───────────────────────────────

function exportProducts() {
  if (!_allProducts.length) { toast('No products to export', true); return; }
  const cols = ['id','name','price','category','stock','status','description'];
  const rows = [cols.join(',')];
  for (const p of _allProducts) {
    rows.push(cols.map(c => {
      const v = p[c] === undefined || p[c] === null ? '' : String(p[c]);
      return v.includes(',') || v.includes('"') ? `"${v.replace(/"/g,'""')}"` : v;
    }).join(','));
  }
  const blob = new Blob([rows.join('\n')], { type: 'text/csv' });
  const url  = URL.createObjectURL(blob);
  const a    = Object.assign(document.createElement('a'), { href: url, download: 'products.csv' });
  a.click();
  URL.revokeObjectURL(url);
  toast('📥 Products exported as CSV');
}

function generateCatalog() {
  if (!_allProducts.length) { toast('No products to catalog', true); return; }
  const name = bizName || 'Our Store';
  const lines = [`📋 *${name} — Product Catalog*\n`];
  _allProducts.forEach((p, i) => {
    lines.push(`${i+1}. *${p.name}* — ${getCurrencySymbol()}${(p.price||0).toFixed(2)}${p.description?' | '+p.description:''}`);
  });
  lines.push(`\nType a product name or number to order! 😊`);
  const text = lines.join('\n');
  navigator.clipboard.writeText(text)
    .then(() => toast('📋 WhatsApp catalog copied!'))
    .catch(() => {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); document.body.removeChild(ta);
      toast('📋 WhatsApp catalog copied!');
    });
}

function shareMenuLink() {
  const base = window.location.origin;
  const link = `${base}/?business=${encodeURIComponent(bizName || '')}`;
  navigator.clipboard.writeText(link)
    .then(() => toast('🔗 Menu link copied!'))
    .catch(() => toast('Link: ' + link));
}

// ── Phase 8: Product analytics ────────────────────────────

async function loadProductAnalytics() {
  const products = _allProducts;

  // Best sellers: products with most orders (use order count from memory if available)
  let orderData = {};
  try {
    const raw = await apiFetch(ROUTES.orders);
    const orders = Array.isArray(raw) ? raw : (raw?.data || []);
    orders.forEach(o => {
      const items = o.items || [];
      items.forEach(item => {
        if (item.name) orderData[item.name] = (orderData[item.name] || 0) + (item.qty || 1);
      });
    });
  } catch(_) {}

  // Best sellers
  const bsEl = document.getElementById('prod-best-sellers');
  if (bsEl) {
    const sorted = [...products].sort((a,b) => (orderData[b.name]||0) - (orderData[a.name]||0)).slice(0,4);
    if (sorted.length) {
      bsEl.innerHTML = sorted.map(p => `
        <div class="prod-analytics-item">
          <span>${escHtml(p.name)}</span>
          <span class="prod-analytics-item-val">${orderData[p.name] ? `${orderData[p.name]} sold` : '—'}</span>
        </div>`).join('');
    } else {
      bsEl.innerHTML = '<div style="color:var(--text-muted,var(--text-dim));">No order data yet.</div>';
    }
  }

  // Recently added
  const raEl = document.getElementById('prod-recently-added');
  if (raEl) {
    const recent = [...products].slice(-4).reverse();
    raEl.innerHTML = recent.map(p => `
      <div class="prod-analytics-item">
        <span>${escHtml(p.name)}</span>
        <span class="prod-analytics-item-val">${getCurrencySymbol()}${(p.price||0).toFixed(2)}</span>
      </div>`).join('') || '<div style="color:var(--text-dim);">No products yet.</div>';
  }

  // Needs attention: OOS or low stock
  const naEl = document.getElementById('prod-needs-attention');
  if (naEl) {
    const attn = products.filter(p => _isProdOos(p) || _isProdLowStock(p));
    naEl.innerHTML = attn.length
      ? attn.map(p => `
          <div class="prod-analytics-item">
            <span>${escHtml(p.name)}</span>
            <span class="prod-analytics-item-val" style="${_isProdOos(p)?'color:var(--red)':'color:var(--amber)'}">
              ${_isProdOos(p)?'Out of stock':'⚠ Low'}
            </span>
          </div>`).join('')
      : '<div style="color:var(--green);font-size:11px;">✅ All products in stock</div>';
  }
}

// ── Phase 11: AI assistant tools ─────────────────────────

function toggleAiTools() {
  const panel = document.getElementById('ai-tools-panel');
  const btn   = document.getElementById('ai-tools-btn');
  if (!panel) return;
  const open = panel.style.display !== 'none';
  panel.style.display = open ? 'none' : '';
  if (btn) btn.style.background = open ? '' : 'rgba(167,139,250,.2)';
}

function _showAiOutput(text) {
  const out = document.getElementById('ai-tools-output');
  if (!out) return;
  out.textContent = text;
  out.style.display = '';
}

function aiGenerateDescription() {
  const name = (document.getElementById('product-name')?.value || '').trim();
  if (!name) { toast('Enter a product name first', true); return; }
  // Placeholder: structured template (real AI endpoint can be wired here)
  const desc = `${name} is a fresh, high-quality item available from our store. `
    + `Order now via WhatsApp and enjoy fast delivery. `
    + `Ask us about today's specials!`;
  const el = document.getElementById('product-description');
  if (el) el.value = desc;
  _showAiOutput(`✍️ Description generated for "${name}":\n\n${desc}`);
  toast('✅ Description applied');
}

function aiSuggestName() {
  const cat  = (document.getElementById('product-category')?.value || 'general').toLowerCase();
  const suggestions = {
    'food & beverage': ['House Special Plate', 'Chef\'s Daily Special', 'Signature Combo'],
    'bakery & pastry': ['Fresh Baked Loaf', 'Daily Pastry Box', 'Artisan Roll'],
    'health & beauty': ['Glow Serum', 'Daily Moisturiser', 'Repair Mask'],
    default: ['Premium Starter Pack', 'Classic Bundle', 'Value Special'],
  };
  const list = suggestions[cat] || suggestions.default;
  _showAiOutput(`💡 Name suggestions for ${cat || 'your category'}:\n\n${list.map((s,i) => `${i+1}. ${s}`).join('\n')}`);
}

function aiPricingAdvice() {
  const price = parseFloat(document.getElementById('product-price')?.value || 0);
  const cat   = (document.getElementById('product-category')?.value || '').toLowerCase();
  if (!price) { toast('Enter a price first', true); return; }
  const margin = cat.includes('food') ? 0.65 : 0.55;
  const cost   = (price * (1 - margin)).toFixed(2);
  const advice = `💲 Pricing analysis for ${getCurrencySymbol()}${price.toFixed(2)}:\n\n`
    + `• Estimated cost at ${(margin*100).toFixed(0)}% margin: ${getCurrencySymbol()}${cost}\n`
    + `• Suggested range: ${getCurrencySymbol()}${(price*0.85).toFixed(2)} – ${getCurrencySymbol()}${(price*1.2).toFixed(2)}\n`
    + `• Consider bundling with complementary items to increase basket size.`;
  _showAiOutput(advice);
}

function aiProductInsights() {
  const total    = _allProducts.length;
  const oos      = _allProducts.filter(_isProdOos).length;
  const low      = _allProducts.filter(_isProdLowStock).length;
  const avgPrice = total ? (_allProducts.reduce((s,p) => s+(p.price||0), 0) / total).toFixed(2) : 0;
  const insights = `📊 Product Performance Insights:\n\n`
    + `• Total products: ${total}\n`
    + `• Average price: ${getCurrencySymbol()}${avgPrice}\n`
    + (oos ? `• ⚠️ ${oos} product${oos>1?'s':''} out of stock — restock to avoid lost sales\n` : `• ✅ All products in stock\n`)
    + (low ? `• ⚠️ ${low} product${low>1?'s':''} running low — consider restocking soon\n` : '')
    + `\nTip: Lower-priced products tend to have higher order frequency.`;
  _showAiOutput(insights);
}

// ── Product image upload via backend ─────────────────────────────────────────
// Sends the file to /products/upload-image (server-side, uses service_role key).
// Returns a public HTTPS URL that WhatsApp can load.
// Falls back to the base64 data URL on error (dashboard preview still works).
async function uploadImageToSupabase(dataUrl, fileName) {
  if (!dataUrl || !dataUrl.startsWith('data:')) return dataUrl;
  try {
    // Convert base64 data URL → Blob → File for multipart upload
    const fetchRes   = await fetch(dataUrl);
    const blob       = await fetchRes.blob();
    const ext        = blob.type.split('/')[1] || 'jpg';
    const file       = new File([blob], (fileName || 'product_' + Date.now()) + '.' + ext, { type: blob.type });

    const formData = new FormData();
    formData.append('file', file);

    // POST to backend — uses Authorization header from existing token
    const resp = await fetch(API + '/products/upload-image', {
      method:  'POST',
      headers: { 'Authorization': `Bearer ${token}` },
      body:    formData,
    });

    if (!resp.ok) {
      const err = await resp.text();
      console.warn('Image upload failed:', err);
      return dataUrl;  // fallback — preview works, WhatsApp won't show image
    }

    const data = await resp.json();
    console.info('Image uploaded:', data.url);
    return data.url;  // public HTTPS URL — works in WhatsApp ✓
  } catch (err) {
    console.warn('Image upload error:', err);
    return dataUrl;  // fallback
  }
}


// Issue 1 fix: Overview customer count now sourced from /crm/segments
// (same source as Customers tab and CRM segment cards) for consistency.
async function loadCustomerStats() {
  try {
    const seg = await apiFetch(ROUTES.crmSegments);
    const _sc = document.getElementById('stat-customers');
    if (_sc && seg && typeof seg.total === 'number') {
      _sc.textContent = seg.total;
    }
  } catch (_) { /* leave existing value on error */ }
}

/* ── #12 HANDOFF DASHBOARD ─────────────────────────────────────────────────── */

async function loadHandoffStats() {
  const _qEl = document.getElementById('hf-queue-body');
  if (_qEl) _qEl.innerHTML = '<div class="empty-state" style="padding:24px;text-align:center;color:var(--text-muted)">Loading…</div>';
  try {
    const data = await apiFetch('/analytics/handoff-stats');
    if (!data) return;

    // KPIs
    const _s = id => document.getElementById(id);
    if (_s('hf-active-count')) _s('hf-active-count').textContent = data.active_count ?? 0;
    if (_s('hf-total-today'))  _s('hf-total-today').textContent  = data.total_today  ?? 0;
    if (_s('hf-agents-active'))_s('hf-agents-active').textContent= (data.agent_activity || []).length;

    // Avg wait — format nicely
    const waitSecs = data.avg_wait_seconds || 0;
    const waitFmt  = waitSecs >= 60
      ? `${Math.floor(waitSecs / 60)}m ${waitSecs % 60}s`
      : (waitSecs > 0 ? `${waitSecs}s` : '—');
    if (_s('hf-avg-wait')) _s('hf-avg-wait').textContent = waitFmt;

    // Badge on nav
    const badge = document.getElementById('nav-handoff-badge');
    if (badge) {
      if (data.active_count > 0) {
        badge.textContent   = data.active_count;
        badge.style.display = '';
      } else {
        badge.style.display = 'none';
      }
    }

    // Active handoff queue
    const queueEl = document.getElementById('hf-queue-body');
    if (queueEl) {
      // UI/UX audit (Phase 8): rows came back in raw DB order — an urgent
      // handoff waiting 2 minutes could sit below a normal one waiting an
      // hour. Sort urgent-first, then longest-waiting-first within each
      // priority, same ordering the (unused) /chat/handoff/queue endpoint
      // already computes server-side — done client-side here instead of
      // switching endpoints, to keep this an additive, response-shape-safe
      // change against the endpoint the dashboard already calls.
      const pending = (data.pending_handoffs || []).slice().sort((a, b) => {
        const aUrgent = (a.priority || 'normal') === 'urgent';
        const bUrgent = (b.priority || 'normal') === 'urgent';
        if (aUrgent !== bUrgent) return aUrgent ? -1 : 1;
        return (b.wait_seconds || 0) - (a.wait_seconds || 0);
      });
      if (pending.length === 0) {
        queueEl.innerHTML = '<div class="empty-state" style="padding:24px;text-align:center;color:var(--text-muted)">✅ No active handoffs</div>';
      } else {
        queueEl.innerHTML = `
          <table class="data-table" style="width:100%;border-collapse:collapse;">
            <thead>
              <tr>
                <th scope="col" style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Priority</th>
                <th scope="col" style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Customer</th>
                <th scope="col" style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Ticket</th>
                <th scope="col" style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Reason</th>
                <th scope="col" style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Waiting</th>
                <th scope="col" style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Action</th>
              </tr>
            </thead>
            <tbody>
              ${pending.map(p => {
                const wait    = p.wait_seconds != null
                  ? (p.wait_seconds >= 60
                    ? `${Math.floor(p.wait_seconds/60)}m ${p.wait_seconds%60}s`
                    : `${p.wait_seconds}s`)
                  : '—';
                const urgency = p.wait_seconds > 300 ? 'color:#ef4444;font-weight:600' : '';
                // UI/UX audit: handoff_priority was already stored per-conversation
                // (set when an agent requests handoff with a reason) but never shown
                // anywhere in this queue — every row looked equally urgent.
                const prio = p.priority || 'normal';
                const prioStyle = prio === 'urgent' ? 'background:rgba(239,68,68,.15);color:#ef4444;'
                  : prio === 'low' ? 'background:rgba(148,163,184,.15);color:var(--text-muted);'
                  : 'background:rgba(56,189,248,.15);color:var(--blue);';
                const prioLabel = prio === 'urgent' ? '🔴 Urgent' : prio === 'low' ? 'Low' : 'Normal';
                return `<tr style="border-top:1px solid var(--border)">
                  <td style="padding:10px 12px;font-size:12px;"><span style="${prioStyle}padding:2px 8px;border-radius:6px;font-family:var(--mono);">${prioLabel}</span></td>
                  <td style="padding:10px 12px">
                    <div style="font-weight:600;font-size:13px">${escHtml(p.customer_name || p.phone)}</div>
                    <div style="font-size:11px;color:var(--text-muted)">${escHtml(p.phone)}</div>
                  </td>
                  <td style="padding:10px 12px;font-size:12px;color:var(--text-muted)">${escHtml(p.ticket || '—')}</td>
                  <td style="padding:10px 12px;font-size:12px">${escHtml(p.handoff_reason || 'Manual')}</td>
                  <td style="padding:10px 12px;font-size:12px;${urgency}">${wait}</td>
                  <td style="padding:10px 12px;white-space:nowrap;">
                    <button class="btn btn-ghost" style="font-size:11px;padding:3px 7px;" onclick="openInboxForHandoffRow('${escHtml(p.phone).replace(/'/g,"\\'")}')" title="Open this chat in Live Inbox">💬 Open</button>
                    <button class="btn btn-ghost" style="font-size:11px;padding:3px 7px;margin-left:4px;" onclick="viewCrmForHandoffRow('${escHtml(p.phone).replace(/'/g,"\\'")}')" title="View CRM record">👤 CRM</button>
                  </td>
                </tr>`;
              }).join('')}
            </tbody>
          </table>`;
      }
    }

    // Agent activity table
    const agentEl = document.getElementById('hf-agent-body');
    if (agentEl) {
      const agents = data.agent_activity || [];
      if (agents.length === 0) {
        agentEl.innerHTML = '<div class="empty-state" style="padding:24px;text-align:center;color:var(--text-muted)">No agent replies sent today</div>';
      } else {
        agentEl.innerHTML = `
          <table class="data-table" style="width:100%;border-collapse:collapse;">
            <thead>
              <tr>
                <th scope="col" style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Agent</th>
                <th scope="col" style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Messages Today</th>
                <th scope="col" style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Last Reply</th>
              </tr>
            </thead>
            <tbody>
              ${agents.map(a => `<tr style="border-top:1px solid var(--border)">
                <td style="padding:10px 12px">
                  <span style="font-weight:600;font-size:13px">👤 ${escHtml(a.agent_name)}</span>
                </td>
                <td style="padding:10px 12px;font-size:13px">${a.messages_today}</td>
                <td style="padding:10px 12px;font-size:12px;color:var(--text-muted)">${fmtTime(a.last_reply_at)}</td>
              </tr>`).join('')}
            </tbody>
          </table>`;
      }
    }
  } catch (e) {
    console.error('loadHandoffStats error:', e);
    // UI/UX audit: this used to fail completely silently (console-only) —
    // the Handoff tab would just sit there stale/blank with no sign anything
    // was wrong and no way to recover except reloading the whole page.
    const queueEl = document.getElementById('hf-queue-body');
    if (queueEl) queueEl.innerHTML = _errorBlock('handoff data', 'loadHandoffStats');
  }
}

// UI/UX audit (Phase 8): the queue had no way to act on a row at all — an
// agent had to separately open Live Inbox and search for the customer by
// hand. Deep-links to the same chat, same pattern openInboxForDrawer() and
// qaViewOrders() already use elsewhere.
function openInboxForHandoffRow(phone) {
  if (!phone) return;
  window.open('/inbox?phone=' + encodeURIComponent(phone), '_blank');
}

// Reuses the CRM drawer directly rather than round-tripping through the
// ?openCrmFor= deep link, since this row is already inside the dashboard.
function viewCrmForHandoffRow(phone) {
  if (!phone) return;
  showSection('crm', null);
  setTimeout(() => {
    const match = (_crmTableData || []).find(c => c.phone === phone);
    if (match) { openCustomerDrawer(match); }
    else { toast('No CRM record for ' + phone + ' yet', true); }
  }, 400);
}



/* ══════════════════════════════════════════════════════════════════════════════
   FEATURE 8 — BUSINESS HEALTH WIDGET
   Additive — loadHealthWidget() called on overview load.
   Checks 4 conditions: WhatsApp connected, products added,
   payment method configured, first order received.
═══════════════════════════════════════════════════════════════════════════════ */

async function loadHealthWidget() {
  const body = document.getElementById('health-widget-body');
  if (!body) return;

  try {
    // Fetch data needed for health checks in parallel
    const [biz, products, orders] = await Promise.all([
      getCachedMe(),
      apiFetch('/products').catch(() => []),
      apiFetch('/orders').catch(() => []),
    ]);

    const checks = [
      {
        key:     'whatsapp',
        label:   'WhatsApp Connected',
        ok:      !!(biz?.whatsapp_phone_id || biz?.use_shared_number),
        guidance:'Connect WhatsApp in Settings → WhatsApp, or use our shared number.',
        action:  "showSection('settings',null);switchSettingsTab('whatsapp',null)",
      },
      {
        key:     'products',
        label:   'Products Added',
        ok:      Array.isArray(products) && products.length > 0,
        guidance:'Add at least one product so customers can order from your bot.',
        action:  "showSection('products',null)",
      },
      {
        key:     'payment',
        label:   'Payment Method Configured',
        ok:      !!(biz?.ecocash_number || biz?.paypal_email || biz?.cash_enabled),
        guidance:'Add a payment method so customers know how to pay.',
        action:  "showSection('settings',null);switchSettingsTab('payment',null)",
      },
      {
        key:     'first_order',
        label:   'First Order Received',
        ok:      Array.isArray(orders) && orders.length > 0,
        guidance:'Share your WhatsApp number or store link to get your first order.',
        action:  "showSection('products',null)",
      },
    ];

    const allOk = checks.every(c => c.ok);
    const score = checks.filter(c => c.ok).length;

    body.innerHTML = `
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;">
        <div style="font-size:28px;font-weight:800;color:${score === 4 ? 'var(--green)' : 'var(--amber)'}">
          ${score}/4
        </div>
        <div>
          <div style="font-size:14px;font-weight:700;">
            ${score === 4 ? '✅ All systems go!' : `${4 - score} item${4 - score > 1 ? 's' : ''} need attention`}
          </div>
          <div style="font-size:12px;color:var(--text-dim);font-family:var(--mono);">
            ${score === 4 ? 'Your AI employee is fully configured.' : 'Complete these steps to go fully live.'}
          </div>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;gap:8px;">
        ${checks.map(c => `
          <div style="display:flex;align-items:flex-start;gap:12px;padding:10px 12px;
                      background:var(--surface2);border-radius:8px;
                      border:1px solid ${c.ok ? 'var(--border)' : 'rgba(245,158,11,0.3)'};">
            <span style="font-size:16px;flex-shrink:0;margin-top:1px">${c.ok ? '✅' : '⚠️'}</span>
            <div style="flex:1;">
              <div style="font-size:13px;font-weight:${c.ok ? '600' : '700'};
                          color:${c.ok ? 'var(--text-dim)' : 'var(--text)'}">
                ${c.label}
              </div>
              ${!c.ok ? `
                <div style="font-size:12px;color:var(--amber);font-family:var(--mono);margin-top:3px;line-height:1.5;">
                  ${c.guidance}
                </div>
                <button onclick="${c.action}" style="margin-top:8px;background:transparent;
                        border:1px solid rgba(245,158,11,0.4);color:var(--amber);border-radius:6px;
                        padding:4px 12px;font-size:11px;cursor:pointer;font-family:var(--mono);">
                  Fix this →
                </button>
              ` : ''}
            </div>
          </div>
        `).join('')}
      </div>
    `;
  } catch (e) {
    body.innerHTML = '<div class="empty">Could not load health status</div>';
  }
}


/* ══════════════════════════════════════════════════════════════════════════════
   FEATURE 9 — FIRST ORDER CELEBRATION
   Additive — checks localStorage to only fire once per business.
   Shows a modal with order details when the first order is detected.
═══════════════════════════════════════════════════════════════════════════════ */

async function checkFirstOrderCelebration() {
  try {
    const bizId  = localStorage.getItem('wazibot_business_id') || '0';
    const seenKey = `wazibot_first_order_seen_${bizId}`;
    if (localStorage.getItem(seenKey)) return;   // already celebrated

    const orders = await apiFetch('/orders').catch(() => []);
    if (!Array.isArray(orders) || orders.length === 0) return;

    // First order exists and hasn't been celebrated yet
    const first = orders[orders.length - 1];   // oldest order (most likely the first)
    const detailEl = document.getElementById('first-order-details');
    if (detailEl) {
      const item = (first.items || [])[0] || {};
      detailEl.innerHTML = `
        <div>📦 Order #${first.id || '—'}</div>
        <div style="margin-top:4px;color:var(--text)">
          ${item.name || 'Order'} — ${getCurrencySymbol()}${parseFloat(first.total_price || 0).toFixed(2)}
        </div>
        <div style="margin-top:4px;">Customer: ${first.customer_phone || '—'}</div>
      `;
    }

    const modal = document.getElementById('first-order-modal');
    if (modal) modal.style.display = 'flex';

    // Remember we've shown this so it doesn't repeat
    localStorage.setItem(seenKey, '1');
  } catch (e) {
    // Non-critical — ignore
  }
}

function dismissFirstOrderModal() {
  const modal = document.getElementById('first-order-modal');
  if (modal) modal.style.display = 'none';
  showSection('orders', null);
}


/* ══════════════════════════════════════════════════════════════════════════════
   FEATURE 10 — GROWTH AUTOMATION SETTINGS UI
   Additive — reads and writes features_json via /me PATCH.
   Does NOT modify growth/cart_recovery.py or growth/reengagement.py.
═══════════════════════════════════════════════════════════════════════════════ */

async function loadGrowthAutomation() {
  try {
    const biz = await apiFetch('/me').catch(() => null);
    const features = biz?.features_json || {};

    // Cart recovery toggle
    const crToggle = document.getElementById('toggle-cart-recovery');
    const crTrack  = document.getElementById('track-cart-recovery');
    if (crToggle) {
      crToggle.checked = !!features.cart_recovery_enabled;
      _applyToggleStyle(crTrack, crToggle.checked);
    }

    // Re-engagement toggle
    const reToggle = document.getElementById('toggle-reengagement');
    const reTrack  = document.getElementById('track-reengagement');
    if (reToggle) {
      reToggle.checked = !!features.reengagement_enabled;
      _applyToggleStyle(reTrack, reToggle.checked);
    }

    // Last run timestamps (stored in features_json if available)
    const crLast = document.getElementById('cart-recovery-last-run');
    if (crLast) crLast.textContent = features.cart_recovery_last_run
      ? new Date(features.cart_recovery_last_run).toLocaleString()
      : 'Never run yet';

    const reLast = document.getElementById('reengagement-last-run');
    if (reLast) reLast.textContent = features.reengagement_last_run
      ? new Date(features.reengagement_last_run).toLocaleString()
      : 'Never run yet';

    // Status labels
    const crStatus = document.getElementById('cart-recovery-status');
    if (crStatus) crStatus.textContent = features.cart_recovery_enabled ? '🟢 Active' : '⚪ Inactive';

    const reStatus = document.getElementById('reengagement-status');
    if (reStatus) reStatus.textContent = features.reengagement_enabled ? '🟢 Active' : '⚪ Inactive';

  } catch (e) {
    console.error('loadGrowthAutomation error:', e);
  }
}

function _applyToggleStyle(track, checked) {
  if (!track) return;
  if (checked) {
    track.style.background    = 'var(--green-glow)';
    track.style.borderColor   = 'var(--green-dim)';
  } else {
    track.style.background    = 'var(--surface2)';
    track.style.borderColor   = 'var(--border)';
  }
  // Move the thumb
  track.style.setProperty('--thumb-translate', checked ? '20px' : '0px');
}

async function saveGrowthSetting(key, enabled) {
  // key: 'cart_recovery' or 'reengagement'
  const trackId = key === 'cart_recovery' ? 'track-cart-recovery' : 'track-reengagement';
  const statusId = key === 'cart_recovery' ? 'cart-recovery-status' : 'reengagement-status';
  const track    = document.getElementById(trackId);
  const statusEl = document.getElementById(statusId);

  _applyToggleStyle(track, enabled);
  if (statusEl) statusEl.textContent = enabled ? '🟢 Active' : '⚪ Inactive';

  try {
    // Read current features_json, patch the relevant key, write back
    const biz      = await apiFetch('/me').catch(() => ({}));
    const features = biz?.features_json || {};
    const featureKey = key === 'cart_recovery' ? 'cart_recovery_enabled' : 'reengagement_enabled';
    features[featureKey] = enabled;

    await apiFetch('/me', {
      method: 'PATCH',
      body: JSON.stringify({ features_json: features }),
    });
    showToast(`${key === 'cart_recovery' ? 'Cart Recovery' : 'Re-engagement'} ${enabled ? 'enabled' : 'disabled'}`);
  } catch (e) {
    showToast('Failed to save setting', true);
  }
}

// Multi-language: toggle services.translation_layer's per-business opt-in
// flag (features_json.translation_enabled). Same pattern as Growth
// Automation above — additive, does not touch any other features_json key.
async function saveTranslationToggle(enabled) {
  try {
    const biz      = await apiFetch('/me').catch(() => ({}));
    const features = biz?.features_json || {};
    features['translation_enabled'] = enabled;

    await apiFetch('/me', {
      method: 'PATCH',
      body: JSON.stringify({ features_json: features }),
    });
    showToast(`Multi-language replies ${enabled ? 'enabled' : 'disabled'}`);
  } catch (e) {
    showToast('Failed to save setting', true);
    // Revert the toggle visually since the save failed
    const el = document.getElementById('set-translation-enabled');
    if (el) el.checked = !enabled;
  }
}

// ── Public Website Generator (Settings → Appearance) ───────────────────────
// Stored in features_json.site_generator — same additive pattern as
// translation_enabled / cart_recovery_enabled above. No schema change.
let _sgSelectedTheme  = 'dark_modern';
let _sgSelectedLayout = 'standard';

function sgSelectTheme(theme) {
  _sgSelectedTheme = theme;
  document.querySelectorAll('.sg-theme-card').forEach(card => {
    card.classList.toggle('active', card.dataset.theme === theme);
  });
  // Update selected label
  const activeCard = document.querySelector(`.sg-theme-card[data-theme="${theme}"]`);
  const labelEl    = document.getElementById('sg-theme-selected-label');
  if (labelEl && activeCard) {
    labelEl.textContent = '✓ ' + (activeCard.querySelector('span')?.textContent || theme) + ' selected';
  }
}

// Filter theme grid by category tab
function sgFilterCategory(tabEl, category) {
  document.querySelectorAll('.sg-cat-tab').forEach(t => t.classList.remove('active'));
  tabEl.classList.add('active');
  document.querySelectorAll('.sg-theme-card').forEach(card => {
    card.style.display = (category === 'all' || card.dataset.cat === category) ? '' : 'none';
  });
}

// Accent colour — preview while dragging colour wheel
function sgPreviewAccent(hex) {
  const el = document.getElementById('sg-accent-preview');
  if (el && hex) el.innerHTML = `Accent: <strong style="color:${hex}">${hex}</strong> — saved with settings`;
  // Highlight matching quick-palette dot
  document.querySelectorAll('.sg-palette-dot').forEach(dot => {
    const bg = dot.style.background || dot.style.backgroundColor;
    dot.classList.toggle('sg-dot-active', bg.toLowerCase() === hex.toLowerCase());
  });
}

// Set accent from quick-palette dot click
function sgSetAccent(hex) {
  const inp = document.getElementById('sg-accent-color');
  const el  = document.getElementById('sg-accent-preview');
  if (!hex) {
    if (inp) inp.value = '#22c55e';
    if (el)  el.textContent = 'Using theme default accent colour';
    document.querySelectorAll('.sg-palette-dot').forEach(d => d.classList.remove('sg-dot-active'));
    return;
  }
  if (inp) inp.value = hex;
  sgPreviewAccent(hex);
}

function sgSelectLayout(layout) {
  _sgSelectedLayout = layout;
  document.querySelectorAll('.sg-layout-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.layout === layout);
  });
}

async function loadSiteGeneratorSettings() {
  try {
    const biz = await getCachedMe();
    if (!biz) return;

    // Build the preview link from the business slug (matches backend
    // _name_to_slug logic: lowercase, non-alphanumerics → hyphens)
    const slug = (biz.name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    const previewLink = document.getElementById('sg-preview-link');
    if (previewLink && slug) previewLink.href = '/site/' + slug;

    const cfg = (biz.features_json && biz.features_json.site_generator) || {};

    sgSelectTheme(cfg.theme_style || 'dark_modern');
    sgSelectLayout(cfg.layout || 'standard');

    const fontSel = document.getElementById('sg-font-select');
    if (fontSel) fontSel.value = cfg.font || 'inter';

    // Restore accent colour
    const _savedAccent = cfg.accent_color || '';
    const _accentInp   = document.getElementById('sg-accent-color');
    if (_accentInp) _accentInp.value = _savedAccent || '#22c55e';
    if (_savedAccent) sgPreviewAccent(_savedAccent);

    _setVal('sg-hours', cfg.business_hours || '');
    _setVal('sg-location', cfg.location || '');

    const showHours    = document.getElementById('sg-show-hours');
    const showLocation = document.getElementById('sg-show-location');
    const showReviews  = document.getElementById('sg-show-reviews');
    const showOrdering = document.getElementById('sg-show-ordering');
    const showMap       = document.getElementById('sg-show-map');
    if (showHours)    showHours.checked    = cfg.show_hours    !== false;
    if (showLocation) showLocation.checked = cfg.show_location !== false;
    if (showReviews)  showReviews.checked  = !!cfg.show_reviews;
    if (showOrdering) showOrdering.checked = cfg.show_ordering !== false;
    if (showMap)      showMap.checked      = !!cfg.show_map;
  } catch (e) {
    console.warn('loadSiteGeneratorSettings:', e.message);
  }
}

async function saveSiteGeneratorSettings() {
  const btn = document.querySelector('[onclick="saveSiteGeneratorSettings()"]');
  try {
    setLoading(btn, true);
    const biz      = await apiFetch('/me').catch(() => ({}));
    const features = biz?.features_json || {};

    const _accentRaw   = (document.getElementById('sg-accent-color')?.value || '').trim();
    const _accentColor = (_accentRaw && _accentRaw !== '#22c55e') ? _accentRaw : '';
    features['site_generator'] = {
      theme_style:     _sgSelectedTheme,
      font:             _getVal('sg-font-select') || 'inter',
      layout:           _sgSelectedLayout,
      accent_color:     _accentColor,
      business_hours:   _getVal('sg-hours') || '',
      location:         _getVal('sg-location') || '',
      show_hours:       document.getElementById('sg-show-hours')?.checked    ?? true,
      show_location:    document.getElementById('sg-show-location')?.checked ?? true,
      show_reviews:     document.getElementById('sg-show-reviews')?.checked  ?? false,
      show_ordering:    document.getElementById('sg-show-ordering')?.checked ?? true,
      show_map:         document.getElementById('sg-show-map')?.checked      ?? false,
    };

    await apiFetch('/me', {
      method: 'PATCH',
      body: JSON.stringify({ features_json: features }),
    });
    invalidateMeCache();
    showToast('✅ Website settings saved');
  } catch (e) {
    showToast('Failed to save website settings: ' + e.message, true);
  } finally {
    setLoading(btn, false);
  }
}


/* ══ HOOK ALL FEATURES INTO EXISTING LOAD FLOWS ═════════════════════════════
   These integrate with the existing showSection() / loadCustomerStats()
   calls. Additive — wrapped in try/except so existing flows never break.
══════════════════════════════════════════════════════════════════════════════ */

// Patch showSection to load Growth Automation when navigated to
const _origShowSection = typeof showSection === 'function' ? showSection : null;
if (_origShowSection) {
  window.showSection = function(name, el) {
    _origShowSection(name, el);
    if (name === 'growth-automation') {
      try { loadGrowthAutomation(); } catch(_) {}
      try { loadGrowthStatus(); } catch(_) {}
    }
    if (name === 'overview') {
      try { loadHealthWidget(); } catch(_) {}
    }
  };
}

// On page load: check for first order celebration (runs once, 2s delay to let data settle)
// Sprint 8: removed — consolidated into _postLoginInit()


/* WAZIBOT-FEATURES-2-3-4-5-6 */

/* ══ F2: REPEAT CUSTOMER METRIC ══════════════════════════════════════════ */
async function loadRepeatCustomerStat() {
  try {
    const data = await apiFetch('/analytics/repeat-customers');
    const rateEl = document.getElementById('stat-repeat-rate');
    const subEl  = document.getElementById('stat-repeat-sub');
    if (data && data.repeat_rate_pct != null) {
      if (rateEl) rateEl.textContent = data.repeat_rate_pct + '%';
      if (subEl)  subEl.textContent  =
        data.repeat_customers + ' of ' + data.total_customers + ' customers reordered';
    } else {
      // Endpoint unavailable or no data — show neutral 0
      if (rateEl) rateEl.textContent = '0%';
      if (subEl)  subEl.textContent  = 'No data yet';
    }
  } catch (e) {
    const rateEl = document.getElementById('stat-repeat-rate');
    if (rateEl) rateEl.textContent = '0%';
  }
}

/* ══ F3: GROWTH STATUS LIVE DATA ═════════════════════════════════════════ */
async function loadGrowthStatus() {
  try {
    const data = await apiFetch('/growth/status');
    if (!data) return;
    const crEnabled = data.cart_recovery?.enabled;
    const crMsgs    = data.cart_recovery?.msgs_sent || 0;
    const crLast    = data.cart_recovery?.last_run;
    const crStatus  = document.getElementById('cart-recovery-status');
    const crLastEl  = document.getElementById('cart-recovery-last-run');
    if (crStatus) crStatus.textContent = crEnabled ? '🟢 Active' : '⚪ Inactive';
    if (crLastEl) crLastEl.textContent = crLast
      ? new Date(crLast).toLocaleString() + (crMsgs ? ' · ' + crMsgs + ' msgs sent' : '')
      : 'Never run yet';
    const reEnabled = data.reengagement?.enabled;
    const reMsgs    = data.reengagement?.msgs_sent || 0;
    const reLast    = data.reengagement?.last_run;
    const reStatus  = document.getElementById('reengagement-status');
    const reLastEl  = document.getElementById('reengagement-last-run');
    if (reStatus) reStatus.textContent = reEnabled ? '🟢 Active' : '⚪ Inactive';
    if (reLastEl) reLastEl.textContent = reLast
      ? new Date(reLast).toLocaleString() + (reMsgs ? ' · ' + reMsgs + ' msgs sent' : '')
      : 'Never run yet';
    const crToggle = document.getElementById('toggle-cart-recovery');
    const reToggle = document.getElementById('toggle-reengagement');
    if (crToggle) crToggle.checked = !!crEnabled;
    if (reToggle) reToggle.checked = !!reEnabled;
  } catch (e) { console.warn('loadGrowthStatus:', e); }
}

/* ══ F4: CSV PRODUCT IMPORT ══════════════════════════════════════════════ */
async function importProductsCSV(input) {
  const file = input?.files?.[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.csv')) {
    showToast('Please select a .csv file', true);
    input.value = '';
    return;
  }
  showToast('⏳ Importing products…');
  try {
    const formData = new FormData();
    formData.append('file', file);
    const res = await apiFetch('/products/import-csv', {
      method: 'POST', body: formData, headers: {},
    });
    if (res?.ok) {
      showToast('✅ Imported ' + res.imported + ' product' + (res.imported !== 1 ? 's' : '') +
                (res.skipped > 0 ? ' · ' + res.skipped + ' skipped' : ''));
      loadProducts();
    } else {
      showToast('Import failed: ' + (res?.detail || 'unknown error'), true);
    }
  } catch (e) {
    showToast('Import error: ' + e.message, true);
  } finally {
    input.value = '';
  }
}

/* ══ F5: UPGRADE PROMPTS ════════════════════════════════════════════════ */
async function checkUpgradePrompts() {
  try {
    const biz = await getCachedMe();
    if (!biz) return;

    // Email-missing banner (one-time, dismissible, session-based) — always show
    const emailBanner = document.getElementById('email-missing-banner');
    if (emailBanner) {
      const dismissed = sessionStorage.getItem('wazi_email_banner_done');
      emailBanner.style.display = (!biz.owner_email && !dismissed) ? 'block' : 'none';
    }

    // Load trial status — single source of truth for banner and prompt visibility
    await loadTrialBanner();

  } catch (e) { /* non-critical */ }
}

// Trial status cache — avoid hammering /trial/status on every section open
let _trialCache = null;
let _trialCacheTs = 0;

async function loadTrialBanner() {
  try {
    // Cache trial status for 5 minutes — it changes at most once a day
    const now = Date.now();
    if (!_trialCache || (now - _trialCacheTs) > 300000) {
      _trialCache = await apiFetch('/trial/status').catch(() => null);
      _trialCacheTs = now;
    }
    const ts = _trialCache;
    if (!ts) return;

    const trialBanner     = document.getElementById('trial-active-banner');
    const expiredBanner   = document.getElementById('trial-expired-banner');
    const restrictedBanner= document.getElementById('trial-restricted-banner');
    const cpBanner        = document.getElementById('upgrade-prompt-campaigns');
    const gpBanner        = document.getElementById('upgrade-prompt-growth');

    // Always hide all three first — prevents more than one showing at once
    if (trialBanner)      trialBanner.style.display      = 'none';
    if (expiredBanner)    expiredBanner.style.display     = 'none';
    if (restrictedBanner) restrictedBanner.style.display  = 'none';

    if (ts.trial_active) {
      // Active trial — show trial banner, HIDE upgrade prompts
      const endsLabel = document.getElementById('trial-ends-label');
      if (endsLabel) {
        endsLabel.textContent = ts.trial_ends_at
          ? ts.trial_ends_at
          : '30 days from signup';
      }
      if (trialBanner)   trialBanner.style.display   = 'flex';
      if (cpBanner)      cpBanner.style.display       = 'none';
      if (gpBanner)      gpBanner.style.display       = 'none';
    } else if (ts.billing_status === 'trialing' || ts.billing_status === 'trial') {
      // Trial expired — either still in the 24h grace window, or fully restricted
      if (ts.restricted) {
        if (restrictedBanner) restrictedBanner.style.display = 'flex';
      } else if (ts.grace_active) {
        if (expiredBanner) {
          expiredBanner.style.display = 'flex';
          const graceMsg = document.getElementById('trial-grace-message');
          if (graceMsg && typeof ts.grace_hours_left === 'number') {
            const h = Math.max(0, Math.round(ts.grace_hours_left));
            graceMsg.textContent = `You have about ${h} hour${h===1?'':'s'} left before some features are restricted. `
              + `Choose a plan from $5.99/month to keep everything running — all your data is safe.`;
          }
        }
      } else if (expiredBanner) {
        expiredBanner.style.display = 'flex';
      }
      if (cpBanner) cpBanner.style.display = 'block';
      if (gpBanner) gpBanner.style.display = 'block';
    } else {
      // Paid or free — hide all trial banners, show upgrade prompts only if free
      const isFree = (ts.effective_tier || '').toLowerCase() === 'free';
      if (cpBanner) cpBanner.style.display = isFree ? 'block' : 'none';
      if (gpBanner) gpBanner.style.display = isFree ? 'block' : 'none';
    }

    // Apply/remove the visual "locked" state on gated sidebar nav items
    _applyNavRestriction(!!ts.restricted);
  } catch (e) { /* non-critical */ }
}

// Grey out gated nav items (Reminders, Conversations, Live Inbox, Handoff,
// Growth, Campaigns) and add a lock icon once restricted=true. Clicking is
// still handled by _navGuard() — this only controls the visual state.
function _applyNavRestriction(restricted) {
  document.querySelectorAll('.nav-item[data-gated="true"]').forEach(el => {
    el.style.opacity = restricted ? '0.45' : '';
    el.style.cursor  = restricted ? 'not-allowed' : '';
    const icon = el.querySelector('.icon');
    if (icon && restricted && !icon.dataset.lockApplied) {
      icon.textContent = '🔒';
      icon.dataset.lockApplied = '1';
    } else if (icon && !restricted && icon.dataset.lockApplied) {
      delete icon.dataset.lockApplied;
      // Original icons are re-set by buildSidebar() on next call; a full
      // page reload after upgrading will restore them naturally.
    }
  });
}

// H5: save owner_email via PATCH /me — called from the banner button
async function saveOwnerEmail() {
  const emailEl = document.getElementById('set-owner-email');
  if (!emailEl) return;
  const email = (emailEl.value || '').trim();
  if (!email || !email.includes('@')) { toast('Enter a valid email address', true); return; }
  try {
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({ owner_email: email }) });
    toast('✅ Email saved');
    sessionStorage.setItem('wazi_email_banner_done', '1');
    invalidateMeCache();
    const banner = document.getElementById('email-missing-banner');
    if (banner) banner.style.display = 'none';
  } catch (e) { toast('Could not save email', true); }
}

/* ══ F6: HEALTH SCORE NUMERIC (extends existing loadHealthWidget) ════════ */
function computeHealthScore(checks) {
  return checks.filter(c => c.ok).length * 25;
}

/* ══ ADDITIONAL DOMContentLoaded HOOKS ══════════════════════════════════ */
// Sprint 8: removed — consolidated into _postLoginInit()


/* ══ Sprint 5: CUSTOMER SATISFACTION SCORE ══════════════════════════════════ */
async function loadSatisfactionScore() {
  try {
    const data = await apiFetch('/analytics/satisfaction');
    if (!data) return;
    const scoreEl = document.getElementById('stat-satisfaction');
    const subEl   = document.getElementById('stat-satisfaction-sub');
    if (scoreEl) {
      scoreEl.textContent = data.avg_rating != null
        ? data.avg_rating.toFixed(1) + ' / 5'
        : '—';
    }
    if (subEl) {
      subEl.textContent = data.rated_count > 0
        ? data.rated_count + ' rating' + (data.rated_count !== 1 ? 's' : '')
        : 'No ratings yet';
    }
  } catch (e) { /* non-critical */ }
}


/* ══ Sprint 6: POST-WIZARD SHARE STORE BANNER ═══════════════════════════════
   Shows a dismissible "Your store is live" banner after wizard completion.
   Uses localStorage so it never re-appears after dismissal.
   Reads /onboarding/progress (existing endpoint) and /me (cached) for slug.
══════════════════════════════════════════════════════════════════════════════ */

const _SHARE_BANNER_KEY = 'wazibot_share_banner_dismissed';

async function showShareStoreBanner() {
  // Bail immediately if already dismissed
  if (localStorage.getItem(_SHARE_BANNER_KEY)) return;

  try {
    // Only show after wizard completion
    const progress = await apiFetch('/onboarding/progress').catch(() => null);
    if (!progress?.completed) return;

    // Build store URL from business name slug
    const biz  = await getCachedMe();
    if (!biz)  return;

    const name = (biz.name || '').trim();
    if (!name) return;

    const slug    = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
    const baseUrl = window.location.origin;
    const storeUrl = `${baseUrl}/store/${slug}`;

    // Populate and show the banner
    const urlEl = document.getElementById('share-store-url');
    if (urlEl) urlEl.textContent = storeUrl;

    const banner = document.getElementById('share-store-banner');
    if (banner) banner.style.display = 'flex';

  } catch (e) {
    // Non-critical — never break the dashboard
  }
}

function dismissShareBanner() {
  const banner = document.getElementById('share-store-banner');
  if (banner) banner.style.display = 'none';
  localStorage.setItem(_SHARE_BANNER_KEY, '1');
}

function copyStoreLink() {
  const urlEl = document.getElementById('share-store-url');
  if (!urlEl) return;
  const url = urlEl.textContent.trim();
  if (!url) return;
  navigator.clipboard.writeText(url).then(() => {
    toast('✅ Store link copied!');
  }).catch(() => {
    // Fallback for browsers that block clipboard
    const input = document.createElement('input');
    input.value = url;
    document.body.appendChild(input);
    input.select();
    document.execCommand('copy');
    document.body.removeChild(input);
    toast('✅ Store link copied!');
  });
}


// Sync customers from chat_messages into user_memory (fixes missing customers
// who have conversations but no completed orders yet).
async function backfillCrm() {
  const btn = event?.target;
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Syncing…'; }
  try {
    const res = await apiFetch('/crm/backfill-from-chats', { method: 'POST' });
    if (res?.ok) {
      toast(`✅ Synced ${res.created} customer${res.created !== 1 ? 's' : ''} from chats`);
      loadCrm();
    } else {
      toast('Sync failed', true);
    }
  } catch (e) {
    toast('Sync error: ' + e.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⚡ Sync from Chats'; }
  }
}
