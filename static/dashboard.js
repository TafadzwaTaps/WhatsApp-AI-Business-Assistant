const API = 'https://wazibot-api-assistant.onrender.com';
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
      if (result.currency_symbol) window.CURRENT_CURRENCY_SYMBOL = result.currency_symbol;
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
      <button class="nav-item admin-item" onclick="showSection('admin-businesses',this);closeSidebar()"><span class="icon">🏢</span> Businesses</button>`;
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
      <button class="nav-item" onclick="showSection('reminders',this);closeSidebar()"><span class="icon">⏳</span> Reminders <span id="nav-rem-badge" class="nav-badge nav-badge-amber" style="display:none"></span></button>
      <button class="nav-item" onclick="showSection('conversations',this);closeSidebar()"><span class="icon">💬</span> Conversations</button>
      <button class="nav-item" onclick="window.open('/inbox','_blank');closeSidebar()"><span class="icon">📥</span> Live Inbox</button>
      <button class="nav-item" onclick="showSection('handoff',this);closeSidebar()"><span class="icon">👤</span> Handoff <span id="nav-handoff-badge" class="nav-badge nav-badge-red" style="display:none"></span></button>
      <button class="nav-item" onclick="showSection('growth-automation',this);closeSidebar()"><span class="icon">🚀</span> Growth</button>
      <button class="nav-item" onclick="showSection('broadcast',this);closeSidebar()"><span class="icon">📢</span> Campaigns</button>
      <button class="nav-item" onclick="showSection('settings',this);closeSidebar()"><span class="icon">⚙️</span> Settings</button>
      <button class="nav-item" onclick="showSection('marketing-kit',this);loadMarketingKit();closeSidebar()"><span class="icon">📣</span> Marketing Kit</button>
      <div class="nav-section">Growth</div>
      <button class="nav-item" onclick="showSection('settings',this);switchSettingsTab('referrals',null);loadReferralTab();closeSidebar()"><span class="icon">🔗</span> Referrals <span id="nav-ref-badge" class="nav-badge nav-badge-green" style="display:none"></span></button>`;
  }
}

function showSection(name, btn) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('section-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
  if (name==='admin-overview'||name==='admin-businesses') loadAdminData();
  if (name==='orders') loadOrders();
  if (name==='products') loadProducts();
  if (name==='conversations') loadConversations();
  if (name==='overview') { loadCustomerStats(); loadRepeatCustomerStat(); try { loadSatisfactionScore(); } catch(_){} try { showShareStoreBanner(); } catch(_){} }
  if (name==='handoff') loadHandoffStats();
  if (name==='broadcast') { loadCustomers(); loadCampaignAudiences(); loadBcCustomerPicker(); updateBcRecipientPreview('all'); }
  if (name==='settings') { loadSettings(); loadTemplates(); }
  if (name==='crm') loadCrm();
  if (name==='reminders') loadReminders();
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

// Alias used by sprint-added functions
const showToast = toast;

// ── SUPERADMIN ────────────────────────────────────────────
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
    const bizList = Array.isArray(businesses) ? businesses : [];
    renderBizTable(bizList, 'sa-biz-overview', false);
    renderBizTable(bizList, 'sa-biz-table', true);
  } catch(e) { toast('Failed to load admin data: ' + e.message, true); }
}

function renderBizTable(businesses, bodyId, showActions) {
  const tbody = document.getElementById(bodyId);
  if (!tbody) return;
  const biz = Array.isArray(businesses) ? businesses : [];
  if (!biz.length) { tbody.innerHTML=`<tr><td colspan="6"><div class="empty">No businesses yet.</div></td></tr>`; return; }
  tbody.innerHTML = biz.map(b => `<tr>
    <td><span class="badge badge-purple">#${b.id}</span></td>
    <td><strong>${escHtml(b.name||'—')}</strong></td>
    <td><span class="badge badge-amber">${escHtml(b.owner_username||'—')}</span></td>
    <td style="color:var(--text-dim);font-size:11px">${escHtml(b.whatsapp_phone_id||'—')}</td>
    <td><span class="badge ${b.is_active?'badge-green':'badge-red'}">${b.is_active?'Active':'Suspended'}</span></td>
    <td>${showActions?`<div style="display:flex;gap:6px;flex-wrap:wrap;">
      <button class="btn btn-ghost" style="color:var(--amber);border-color:rgba(245,158,11,0.3);" onclick="toggleBiz(${b.id},${b.is_active})">${b.is_active?'Suspend':'Activate'}</button>
      <button class="btn btn-ghost" onclick="deleteBiz(${b.id})">✕ Delete</button>
    </div>`:fmtTime(b.created_at || b.createdAt || b.timestamp)}</td>
  </tr>`).join('');
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
      if (el) el.innerHTML=`<tr><td colspan="7"><div class="empty">⚠ ${e.message}</div></td></tr>`;
    });
  }
}

function renderOrders(orders, bodyId, showStatus) {
  const cols = showStatus ? 7 : 6;
  const tbody = document.getElementById(bodyId);
  if (!tbody) return;
  const rows = Array.isArray(orders) ? orders : [];
  if (!rows.length){tbody.innerHTML=`<tr><td colspan="${cols}"><div class="empty">No orders yet.</div></td></tr>`;return;}
  tbody.innerHTML=rows.map(o=>{
    const status = o.status || 'pending';
    return `<tr>
    <td><span class="badge badge-amber">#${o.id||'—'}</span></td>
    <td>${escHtml(o.customer_phone||'—')}</td>
    <td>${escHtml(o.product_name||'—')}</td>
    <td>${o.quantity||0}</td>
    <td><span class="badge badge-green">${getCurrencySymbol()}${(o.total_price||0).toFixed(2)}</span></td>
    ${showStatus?`<td><span class="badge ${status==='pending'?'badge-amber':'badge-green'}">${escHtml(status)}</span></td>`:''}
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

    // Apply any active filters then render
    applyProductFilters();

    // Load analytics after products (Phase 8)
    loadProductAnalytics();
  } catch(e) {
    if (skeleton) skeleton.style.display = 'none';
    if (tbody) tbody.innerHTML = `<tr><td colspan="8"><div class="empty">⚠ ${e.message}</div></td></tr>`;
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
  if (p.status === 'out_of_stock') return true;
  if (typeof p.stock === 'number' && p.stock === 0) return true;
  return false;
}
function _isProdLowStock(p) {
  return typeof p.stock === 'number' && p.stock > 0 && p.stock <= 5;
}
function _prodStatusBadge(p) {
  if (_isProdOos(p))         return `<span class="prod-status-oos">● Out of Stock</span>`;
  if (_isProdLowStock(p))    return `<span class="prod-status-low">⚠ Low Stock</span>`;
  if (p.status === 'draft')  return `<span class="prod-status-draft">○ Draft</span>`;
  return `<span class="prod-status-active">● Active</span>`;
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
    const stockDisplay = typeof p.stock === 'number'
      ? `<span style="font-size:12px;font-family:var(--mono);${p.stock <= 5 ? 'color:var(--amber)' : ''}">${p.stock}</span>`
      : `<span style="color:var(--text-dim);font-size:11px;">—</span>`;
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
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1;text-align:center;padding:24px;">📦 No products found</div>';
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
        ${typeof p.stock === 'number' ? `<div style="font-size:10px;font-family:var(--mono);color:${p.stock<=5?'var(--amber)':'var(--text-dim)'};margin-top:3px;">${p.stock<=5&&p.stock>0?'⚠ Low: ':''}${p.stock === 0?'Out of stock':`${p.stock} in stock`}</div>` : ''}
        <div style="display:flex;gap:6px;margin-top:8px;">
          <button class="btn btn-ghost" style="flex:1;font-size:10px;padding:5px;" onclick="event.stopPropagation();openProdEdit(${p.id})">✎ Edit</button>
          <button class="btn btn-ghost" style="flex:1;font-size:10px;padding:5px;" onclick="event.stopPropagation();deleteProduct(${p.id})">✕</button>
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

async function deleteProduct(id) {
  try {
    await apiFetch(`${ROUTES.products}/${id}`, { method: 'DELETE' });
    toast('Product removed');
    loadProducts();
  } catch(e) { toast('Failed to remove product', true); }
}

// ── CONVERSATIONS ─────────────────────────────────────────
async function loadConversations() {
  try {
    const convos = await apiFetch(ROUTES.conversations);
    if (!convos) return;
    const list_data = Array.isArray(convos) ? convos : (convos.data || []);
    // stat-customers is now populated from /crm/segments (loadCustomerStats)
    // for consistency with the Customers tab — do not overwrite it here.
    const list = document.getElementById('contact-list');
    if (!list_data.length){list.innerHTML='<div class="empty">No conversations yet.</div>';return;}
    list.innerHTML=list_data.map(c=>{
      const phone = c.phone || c.customer_phone || '—';
      const lastMsg = c.last_message || c.message || '';
      const lastDir = c.last_direction || c.direction || '';
      const lastAt = c.last_message_at || c.created_at || c.timestamp || null;
      const unread = c.unread_count || 0;
      return `<div class="contact-item ${phone===activePhone?'active':''}" onclick="openChat('${phone}',this)">
      <div class="contact-phone">${phone}${unread>0?` <span class="badge badge-green">${unread}</span>`:''}</div>
      <div class="contact-preview">${lastDir==='incoming'||lastDir==='in'?'👤':'🤖'} ${escHtml(lastMsg)}</div>
      <div class="contact-time">${fmtTime(lastAt)}</div>
    </div>`;
    }).join('');
  } catch(e){ const _cl=document.getElementById('contact-list'); if(_cl) _cl.innerHTML=`<div class="empty">⚠ ${e.message}</div>`; }
}

async function openChat(phone, el) {
  if (!phone) return;
  activePhone = phone;
  document.querySelectorAll('.contact-item').forEach(i => i.classList.remove('active'));
  if (el) el.classList.add('active');

  const win = document.getElementById('chat-window');
  if (!win) return;

  win.innerHTML = `
    <div class="chat-header" style="display:flex;align-items:center;">
      Chat with <span style="margin-left:6px;">${escHtml(phone)}</span>
      <button class="panel-action" style="margin-left:auto;font-size:11px;" onclick="openChat('${escHtml(phone).replace(/'/g,"\\'")}',null)">↻</button>
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
}

async function loadChatMessages(phone) {
  const msgsEl = document.getElementById('chat-msgs');
  if (!msgsEl) return;
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
    msgsEl.scrollTop = msgsEl.scrollHeight;
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

    if (result) {
      result.style.display = 'block';
      result.className = 'broadcast-result ' + (ok ? 'success' : 'error');
      result.innerHTML = ok
        ? `✅ Sent to <strong>${data.sent}</strong> customer${data.sent !== 1 ? 's' : ''}!`
        : `Sent: <strong>${data.sent}</strong>  |  Failed: <strong>${data.failed}</strong>` +
          (data.failed_numbers && data.failed_numbers.length
            ? `<div style="font-size:10px;margin-top:6px;opacity:0.7;">Failed: ${data.failed_numbers.slice(0,5).join(', ')}${data.failed_numbers.length > 5 ? '…' : ''}</div>`
            : '');
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
  } catch(e) { console.warn('loadSettings /me:', e.message); }

  // Load payment settings
  try {
    const pay = await apiFetch('/me/payment-settings');
    if (pay) {
      _setVal('set-ecocash-number', pay.ecocash_number || '');
      _setVal('set-ecocash-name',   pay.ecocash_name   || '');
      _setVal('set-paypal-email',   pay.paypal_email   || '');
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
      currency:        _getVal('set-currency'),
      currency_symbol: currSym,
      owner_email:     _ownerEmail,
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
          ? `✅ ${days} trial day${days!==1?'s':''} remaining. Upgrade from $1.99/mo when ready.`
          : `Trial ended. Upgrade from $1.99/month to restore full access.`;
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

async function stripeUpgrade() {
  window.location.href = '/static/pricing.html';
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

async function loadMarketingQR() {
  const img = document.getElementById('mkt-qr-img');
  const loading = document.getElementById('mkt-qr-loading');
  const errEl   = document.getElementById('mkt-qr-error');
  const dlBtn   = document.getElementById('mkt-qr-download');
  if (!img) return;
  try {
    const token = localStorage.getItem('wazi_token') || '';
    const resp  = await fetch('/marketing/qr', { headers: { 'Authorization': `Bearer ${token}` } });
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
    const resp  = await fetch('/marketing/qr/download', { headers: { 'Authorization': `Bearer ${token}` } });
    if (!resp.ok) throw new Error('Download failed');
    const blob  = await resp.blob();
    const url   = URL.createObjectURL(blob);
    const fname = (_mktData?.slug || 'business') + '-whatsapp-qr.png';
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

  // Load currency symbol BEFORE any money rendering, so the first paint of
  // Orders/Products/stats is correct rather than briefly flashing '$' and
  // never refreshing (this was the root cause of currency "not applying
  // across the system" — it simply wasn't fetched early/at all).
  if (token) { try { await getCachedMe(); } catch (_) {} }

  if(userRole==='superadmin'){loadAdminData();}
  else{loadOrders();loadProducts();loadConversations();loadCustomerStats();}
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
  init();
} else if (!token && refreshTok && userRole) {
  // Access token expired on load — try silent refresh before showing login
  (async () => {
    const ok = await tryRefresh();
    if (ok) {
      const _ls5 = document.getElementById('login-screen');
      if (_ls5) _ls5.style.display = 'none';
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
  } catch (_) {}

  // Load inactive count (30d) for the 4th card
  try {
    const inactive = await apiFetch(ROUTES.crmInactive + '?days=30');
    const el = document.getElementById('crm-count-inactive');
    if (el) el.textContent = Array.isArray(inactive) ? inactive.length : '—';
  } catch (_) {}

  // Load simple customer list
  const tbody = document.getElementById('crm-table-body');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="6"><div class="empty">Loading…</div></td></tr>';
  try {
    const rows = await apiFetch(ROUTES.crmSegments + '/all');
    const data = Array.isArray(rows) ? rows : [];
    _crmTableData = data;   // cache for openCustomerDrawer(index) — avoids unsafe JSON-in-HTML-attribute
    if (!data.length) {
      tbody.innerHTML = '<tr><td colspan="6"><div class="empty">No customers yet.</div></td></tr>';
      return;
    }
    tbody.innerHTML = data.map((c, i) => {
      const seg = getSegmentLabel(c.order_count || 0, c.total_spent || 0);
      return `<tr>
        <td style="font-family:var(--mono);font-size:12px;">${escHtml(c.phone || '—')}</td>
        <td>${escHtml(c.customer_name || '—')}</td>
        <td>${c.order_count || 0}</td>
        <td style="color:var(--green);">${getCurrencySymbol()}${parseFloat(c.total_spent || 0).toFixed(2)}</td>
        <td style="font-family:var(--mono);font-size:11px;color:var(--text-dim);">${c.last_seen ? fmtTime(c.last_seen) : '—'}</td>
        <td><button class="btn btn-ghost" style="font-size:11px;padding:3px 8px;" onclick="openCustomerDrawer(_crmTableData[${i}])">View</button></td>
      </tr>`;
    }).join('');
  } catch (e) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="6"><div class="empty">⚠ ${e.message}</div></td></tr>`;
  }
}

// kept for compatibility (called by overview card click)
async function loadCrmSegment(segment) {
  showSection('crm', null);
  loadCrm();
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
  document.getElementById('customer-drawer').classList.add('open');
  document.getElementById('drawer-overlay').classList.add('open');
  loadDrawerOrders(phone);
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
}

function openInboxForDrawer() {
  if (_drawerCustomer && _drawerCustomer.phone) {
    window.open('/inbox', '_blank');
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
    if (tbody) tbody.innerHTML = `<tr><td colspan="7"><div class="empty">⚠ ${e.message}</div></td></tr>`;
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
  const filtered = status === 'all' ? _ordersData : _ordersData.filter(o => o.status === status);
  renderOrders(filtered, 'orders-body', true);
  if (_ordersView === 'kanban') renderKanban(_ordersData);
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
      ? colOrders.map(o => `
        <div class="kanban-card" onclick="updateOrderStatus(${o.id})">
          <div class="kanban-card-id">#${o.id}</div>
          <div class="kanban-card-name">${escHtml(o.customer_phone||'—')}</div>
          <div class="kanban-card-total">${getCurrencySymbol()}${parseFloat(o.total_price||0).toFixed(2)}</div>
          <div class="kanban-card-time">${fmtTime(o.created_at)}</div>
        </div>`).join('')
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
      if (el) el.innerHTML=`<tr><td colspan="7"><div class="empty">⚠ ${e.message}</div></td></tr>`;
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

// Hook analytics load into overview — only when logged in
document.addEventListener('DOMContentLoaded', () => {
  setTimeout(() => { if (token) loadAnalyticsCharts(); }, 500);
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
    bar.innerHTML = `
      <span id="bulk-count">0 selected</span>
      <button class="bulk-btn green" onclick="bulkUpdateStatus('confirmed')">✅ Preparing</button>
      <button class="bulk-btn amber" onclick="bulkUpdateStatus('ready')">🎉 Ready</button>
      <button class="bulk-btn green" onclick="bulkUpdateStatus('delivered')">📦 Delivered</button>
      <button class="bulk-btn red"   onclick="bulkUpdateStatus('cancelled')">❌ Cancel</button>
      <button class="bulk-btn-cancel" onclick="clearBulkSelect()">✕ Clear</button>
    `;
    const ordersSection = document.getElementById('orders-section') || document.querySelector('[data-section="orders"]');
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
    renderOrderFilters();
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

// Inject help FAB into dashboard if not present
(function() {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectDashboardHelp);
  } else {
    injectDashboardHelp();
  }
})();

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
      <button class="help-panel-close" onclick="closeDashHelp()">✕</button>
    </div>
    <div class="help-panel-input-row">
      <input class="help-panel-input" id="dash-help-input" placeholder="How do I…?" onkeydown="if(event.key==='Enter')askDashHelp()">
      <button class="help-panel-send" onclick="askDashHelp()">→</button>
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
  } catch (e) {
    console.warn('Referral load failed:', e.message);
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
  document.getElementById('edit-prod-desc').value  = p.description || '';
  const statusEl = document.getElementById('edit-prod-status');
  if (statusEl) statusEl.value = p.status || 'active';
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
      const pending = data.pending_handoffs || [];
      if (pending.length === 0) {
        queueEl.innerHTML = '<div class="empty-state" style="padding:24px;text-align:center;color:var(--text-muted)">✅ No active handoffs</div>';
      } else {
        queueEl.innerHTML = `
          <table class="data-table" style="width:100%;border-collapse:collapse;">
            <thead>
              <tr>
                <th style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Customer</th>
                <th style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Ticket</th>
                <th style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Reason</th>
                <th style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Waiting</th>
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
                return `<tr style="border-top:1px solid var(--border)">
                  <td style="padding:10px 12px">
                    <div style="font-weight:600;font-size:13px">${escHtml(p.customer_name || p.phone)}</div>
                    <div style="font-size:11px;color:var(--text-muted)">${escHtml(p.phone)}</div>
                  </td>
                  <td style="padding:10px 12px;font-size:12px;color:var(--text-muted)">${escHtml(p.ticket || '—')}</td>
                  <td style="padding:10px 12px;font-size:12px">${escHtml(p.handoff_reason || 'Manual')}</td>
                  <td style="padding:10px 12px;font-size:12px;${urgency}">${wait}</td>
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
                <th style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Agent</th>
                <th style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Messages Today</th>
                <th style="text-align:left;padding:8px 12px;color:var(--text-muted);font-size:11px;">Last Reply</th>
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
  }
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

    _setVal('sg-hours', cfg.business_hours || '');
    _setVal('sg-location', cfg.location || '');

    const showHours    = document.getElementById('sg-show-hours');
    const showLocation = document.getElementById('sg-show-location');
    const showReviews  = document.getElementById('sg-show-reviews');
    const showOrdering = document.getElementById('sg-show-ordering');
    if (showHours)    showHours.checked    = cfg.show_hours    !== false;
    if (showLocation) showLocation.checked = cfg.show_location !== false;
    if (showReviews)  showReviews.checked  = !!cfg.show_reviews;
    if (showOrdering) showOrdering.checked = cfg.show_ordering !== false;
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

    features['site_generator'] = {
      theme_style:     _sgSelectedTheme,
      font:             _getVal('sg-font-select') || 'inter',
      layout:           _sgSelectedLayout,
      business_hours:   _getVal('sg-hours') || '',
      location:         _getVal('sg-location') || '',
      show_hours:       document.getElementById('sg-show-hours')?.checked    ?? true,
      show_location:    document.getElementById('sg-show-location')?.checked ?? true,
      show_reviews:     document.getElementById('sg-show-reviews')?.checked  ?? false,
      show_ordering:    document.getElementById('sg-show-ordering')?.checked ?? true,
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

    const trialBanner  = document.getElementById('trial-active-banner');
    const expiredBanner= document.getElementById('trial-expired-banner');
    const cpBanner     = document.getElementById('upgrade-prompt-campaigns');
    const gpBanner     = document.getElementById('upgrade-prompt-growth');

    // Always hide both first — prevents both showing simultaneously
    if (trialBanner)   trialBanner.style.display   = 'none';
    if (expiredBanner) expiredBanner.style.display  = 'none';

    if (ts.trial_active) {
      // Active trial — show trial banner, HIDE upgrade prompts
      const endsLabel = document.getElementById('trial-ends-label');
      if (endsLabel) {
        endsLabel.textContent = ts.trial_ends_at
          ? ts.trial_ends_at
          : '30 days from signup';
      }
      if (trialBanner)   trialBanner.style.display   = 'flex';
      if (expiredBanner) expiredBanner.style.display  = 'none';
      if (cpBanner)      cpBanner.style.display       = 'none';
      if (gpBanner)      gpBanner.style.display       = 'none';
    } else if (ts.billing_status === 'trialing' || ts.billing_status === 'trial') {
      // Expired trial — hide trial banner, show upgrade prompts
      if (trialBanner)   trialBanner.style.display   = 'none';
      if (expiredBanner) expiredBanner.style.display  = 'flex';
      if (cpBanner)      cpBanner.style.display       = 'block';
      if (gpBanner)      gpBanner.style.display       = 'block';
    } else {
      // Paid or free — hide both trial banners, show upgrade prompts only if free
      if (trialBanner)   trialBanner.style.display   = 'none';
      if (expiredBanner) expiredBanner.style.display  = 'none';
      const isFree = (ts.effective_tier || '').toLowerCase() === 'free';
      if (cpBanner) cpBanner.style.display = isFree ? 'block' : 'none';
      if (gpBanner) gpBanner.style.display = isFree ? 'block' : 'none';
    }
  } catch (e) { /* non-critical */ }
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
