/* ═══════════════════════════════════════════════════════════
   WaziBot Dashboard — dashboard.js
   FIX-1  saveSession() always stores refresh_token + business_id
   FIX-2  tryRefresh() sends refresh_token in body (matches /auth/refresh)
   FIX-3  apiFetch() retries once after a successful token refresh
   FIX-4  logout() redirects to / (not just hides panel) so stale state
          is cleared
   FIX-5  Live Inbox link uses /inbox route (not /static/inbox.html)
   ═══════════════════════════════════════════════════════════ */

'use strict';

const API = 'https://wazibot-api-assistant.onrender.com';

const ROUTES = {
  login:         '/auth/login',
  register:      '/auth/signup',
  refresh:       '/auth/refresh',          // FIX-1: endpoint now exists
  adminStats:    '/admin/stats',
  adminBiz:      '/admin/businesses',
  products:      '/products',
  orders:        '/orders',
  conversations: '/chat/conversations',
  customers:     '/customers',
  broadcast:     '/broadcast',
};

// ── SESSION STATE ─────────────────────────────────────────
let token       = localStorage.getItem('wazi_token');
let refreshTok  = localStorage.getItem('wazi_refresh');
let userRole    = localStorage.getItem('wazi_role');
let userName    = localStorage.getItem('wazi_user');
let bizName     = localStorage.getItem('wazi_biz');
let bizId       = parseInt(localStorage.getItem('wazi_business_id') || '0');
let activePhone = null;
let customerPhones = [];

// ── MOBILE SIDEBAR ────────────────────────────────────────
function toggleSidebar() {
  const sidebar = document.querySelector('.sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  if (sidebar) sidebar.classList.toggle('open');
  if (overlay) overlay.classList.toggle('open');
}

function closeSidebar() {
  const sidebar = document.querySelector('.sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  if (sidebar) sidebar.classList.remove('open');
  if (overlay) overlay.classList.remove('open');
}

// ── AUTH ──────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.login-tab').forEach((t, i) =>
    t.classList.toggle('active', (i === 0 && tab === 'login') || (i === 1 && tab === 'register'))
  );
  document.getElementById('tab-login').classList.toggle('active', tab === 'login');
  document.getElementById('tab-register').classList.toggle('active', tab === 'register');
  const err = document.getElementById('login-error');
  if (err) err.textContent = '';
}

async function doLogin() {
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  const errEl    = document.getElementById('login-error');
  errEl.textContent = '';
  if (!username || !password) { errEl.textContent = 'Enter credentials'; return; }
  try {
    const res = await fetch(API + ROUTES.login, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ username, password }),
    });
    if (!res.ok) { const d = await res.json(); errEl.textContent = d.detail || 'Login failed'; return; }
    const data = await res.json();
    saveSession(data, username);
    const ls = document.getElementById('login-screen');
    if (ls) ls.style.display = 'none';
    if (window.location.pathname !== '/dashboard') {
      window.location.href = '/dashboard';
      return;
    }
    init();
  } catch (e) { errEl.textContent = 'Cannot reach server. Is backend running?'; }
}

async function doRegister() {
  const errEl = document.getElementById('login-error');
  errEl.textContent = '';
  const payload = {
    business_name:    document.getElementById('reg-bizname').value.trim(),
    username:         document.getElementById('reg-username').value.trim(),
    password:         document.getElementById('reg-password').value,
    whatsapp_phone_id: (document.getElementById('reg-phoneid') || {}).value?.trim() || null,
  };
  if (!payload.business_name || !payload.username || !payload.password) {
    errEl.textContent = 'All fields are required'; return;
  }
  try {
    const res = await fetch(API + ROUTES.register, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
    });
    if (!res.ok) {
      const d = await res.json();
      errEl.textContent = Array.isArray(d.detail)
        ? d.detail.map(e => e.msg).join(' • ')
        : (d.detail || 'Registration failed');
      return;
    }
    const data = await res.json();
    saveSession(data, payload.username);
    const ls = document.getElementById('login-screen');
    if (ls) ls.style.display = 'none';
    init();
  } catch (e) { errEl.textContent = 'Cannot reach server'; }
}

// FIX-1: always persist refresh_token and business_id
function saveSession(data, username) {
  token      = data.access_token  || token;
  refreshTok = data.refresh_token || null;
  userRole   = data.role          || userRole;
  userName   = username           || userName;
  bizName    = data.business_name || bizName || '';
  bizId      = data.business_id   || bizId   || 0;

  localStorage.setItem('wazi_token',       token       || '');
  localStorage.setItem('wazi_refresh',     refreshTok  || '');
  localStorage.setItem('wazi_role',        userRole    || '');
  localStorage.setItem('wazi_user',        userName    || '');
  localStorage.setItem('wazi_biz',         bizName     || '');
  localStorage.setItem('wazi_business_id', bizId       || '');
}

// FIX-2: sends { refresh_token } body — matches backend /auth/refresh
async function tryRefresh() {
  if (!refreshTok) { logout(); return false; }
  try {
    const res = await fetch(API + ROUTES.refresh, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ refresh_token: refreshTok }),
    });
    if (!res.ok) { logout(); return false; }
    const data = await res.json();
    saveSession(data, userName);
    return true;
  } catch { logout(); return false; }
}

// FIX-4: full redirect so stale local state is flushed
function logout() {
  ['wazi_token','wazi_refresh','wazi_role','wazi_user','wazi_biz','wazi_business_id']
    .forEach(k => localStorage.removeItem(k));
  token = refreshTok = userRole = userName = bizName = null;
  bizId = 0;
  window.location.href = '/';
}

// ── API ───────────────────────────────────────────────────
// FIX-3: single retry after refresh — never infinite loop
async function apiFetch(path, opts = {}, _retried = false) {
  try {
    const res = await fetch(API + path, {
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      ...opts,
    });

    if (res.status === 401 && !_retried) {
      const ok = await tryRefresh();
      if (ok) return apiFetch(path, opts, true);   // one retry with new token
      return null;
    }

    if (!res.ok) {
      let msg = res.statusText || 'Request failed';
      try { const e = await res.json(); msg = e.detail || msg; } catch {}
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
  const fu = document.getElementById('footer-user');
  if (fu) fu.textContent = userName || '';
  const nav = document.getElementById('sidebar-nav');
  if (!nav) return;

  if (userRole === 'superadmin') {
    const rl = document.getElementById('sidebar-role-label'); if (rl) rl.textContent = 'Super Admin';
    const rb = document.getElementById('sidebar-role-badge'); if (rb) rb.innerHTML = '<span class="badge badge-purple">SUPERADMIN</span>';
    nav.innerHTML = `
      <div class="nav-section">Platform</div>
      <button class="nav-item admin-item active" onclick="showSection('admin-overview',this);closeSidebar()"><span class="icon">🌐</span> Overview <span class="status-dot"></span></button>
      <button class="nav-item admin-item" onclick="showSection('admin-businesses',this);closeSidebar()"><span class="icon">🏢</span> Businesses</button>`;
  } else {
    const rl = document.getElementById('sidebar-role-label'); if (rl) rl.textContent = bizName || 'Business';
    const rb = document.getElementById('sidebar-role-badge'); if (rb) rb.innerHTML = '<span class="badge badge-green">BUSINESS</span>';
    const bnh = document.getElementById('biz-name-header'); if (bnh && bizName) bnh.textContent = '🟢 ' + bizName;
    // FIX-5: /inbox route instead of /static/inbox.html
    nav.innerHTML = `
      <div class="nav-section">Dashboard</div>
      <button class="nav-item active" onclick="showSection('overview',this);closeSidebar()"><span class="icon">📊</span> Overview <span class="status-dot"></span></button>
      <button class="nav-item" onclick="showSection('orders',this);closeSidebar()"><span class="icon">🛒</span> Orders</button>
      <button class="nav-item" onclick="showSection('products',this);closeSidebar()"><span class="icon">📦</span> Products</button>
      <button class="nav-item" onclick="showSection('conversations',this);closeSidebar()"><span class="icon">💬</span> Conversations</button>
      <button class="nav-item" onclick="window.open('/inbox','_blank');closeSidebar()"><span class="icon">📥</span> Live Inbox</button>
      <button class="nav-item" onclick="showSection('broadcast',this);closeSidebar()"><span class="icon">📢</span> Broadcast</button>
      <button class="nav-item" onclick="showSection('settings',this);closeSidebar()"><span class="icon">⚙️</span> Settings</button>`;
  }
}

function showSection(name, btn) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const sec = document.getElementById('section-' + name);
  if (sec) sec.classList.add('active');
  if (btn) btn.classList.add('active');
  if (name === 'admin-overview' || name === 'admin-businesses') loadAdminData();
  if (name === 'orders')         loadOrders();
  if (name === 'products')       loadProducts();
  if (name === 'conversations')  loadConversations();
  if (name === 'broadcast')      loadCustomers();
  if (name === 'settings')       loadSettings();
}

// ── ADMIN ─────────────────────────────────────────────────
async function loadAdminData() {
  try {
    const [stats, bizList] = await Promise.all([
      apiFetch(ROUTES.adminStats),
      apiFetch(ROUTES.adminBiz),
    ]);
    if (stats) {
      const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
      set('stat-businesses', stats.businesses || 0);
      set('stat-active',     stats.active_businesses || 0);
      set('stat-orders',     stats.total_orders || 0);
      set('stat-revenue',    `$${(stats.total_revenue || 0).toFixed(2)}`);
    }
    if (bizList) renderBusinessList(bizList);
  } catch (e) { toast('Admin load failed: ' + e.message, true); }
}

function renderBusinessList(list) {
  const el = document.getElementById('biz-table-body');
  if (!el) return;
  if (!list.length) { el.innerHTML = '<tr><td colspan="5" class="empty">No businesses yet.</td></tr>'; return; }
  el.innerHTML = list.map(b => `
    <tr>
      <td>${b.id}</td>
      <td>${escHtml(b.name)}</td>
      <td>${escHtml(b.owner_username)}</td>
      <td><span class="badge ${b.is_active ? 'badge-green' : 'badge-red'}">${b.is_active ? 'Active' : 'Suspended'}</span></td>
      <td>
        <button class="panel-action" onclick="toggleBiz(${b.id},${!b.is_active})">${b.is_active ? 'Suspend' : 'Activate'}</button>
        <button class="panel-action" style="color:var(--red)" onclick="deleteBiz(${b.id})">Delete</button>
      </td>
    </tr>`).join('');
}

async function toggleBiz(id, active) {
  try {
    await apiFetch(`${ROUTES.adminBiz}/${id}`, { method: 'PATCH', body: JSON.stringify({ is_active: active }) });
    toast(active ? '✅ Activated' : '⏸ Suspended');
    loadAdminData();
  } catch (e) { toast(e.message, true); }
}

async function deleteBiz(id) {
  if (!confirm('Delete this business and all its data?')) return;
  try {
    await apiFetch(`${ROUTES.adminBiz}/${id}`, { method: 'DELETE' });
    toast('🗑 Deleted');
    loadAdminData();
  } catch (e) { toast(e.message, true); }
}

// ── PRODUCTS ──────────────────────────────────────────────
async function loadProducts() {
  try {
    const data = await apiFetch(ROUTES.products);
    if (!data) return;
    const el = document.getElementById('product-list');
    if (!el) return;
    if (!data.length) { el.innerHTML = '<div class="empty">No products yet.</div>'; return; }
    el.innerHTML = data.map(p => `
      <div class="product-card">
        ${p.image_url ? `<img src="${escHtml(p.image_url)}" alt="${escHtml(p.name)}" style="width:100%;border-radius:8px;margin-bottom:8px;">` : ''}
        <div class="product-name">${escHtml(p.name)}</div>
        <div class="product-price">$${p.price.toFixed(2)}</div>
        <button class="panel-action" style="color:var(--red);margin-top:8px;" onclick="deleteProduct(${p.id})">🗑 Remove</button>
      </div>`).join('');
  } catch (e) { toast('Products load failed: ' + e.message, true); }
}

async function addProduct() {
  const name  = (document.getElementById('prod-name')  || {}).value?.trim();
  const price = parseFloat((document.getElementById('prod-price') || {}).value || '0');
  const img   = (document.getElementById('prod-img')   || {}).value?.trim() || null;
  if (!name || isNaN(price) || price < 0) { toast('Enter a valid name and price', true); return; }
  try {
    await apiFetch(ROUTES.products, { method: 'POST', body: JSON.stringify({ name, price, image_url: img }) });
    toast('✅ Product added');
    const ni = document.getElementById('prod-name');  if (ni) ni.value = '';
    const pi = document.getElementById('prod-price'); if (pi) pi.value = '';
    const ii = document.getElementById('prod-img');   if (ii) ii.value = '';
    loadProducts();
  } catch (e) { toast('Add failed: ' + e.message, true); }
}

async function deleteProduct(id) {
  if (!confirm('Delete this product?')) return;
  try {
    await apiFetch(`${ROUTES.products}/${id}`, { method: 'DELETE' });
    toast('🗑 Product deleted');
    loadProducts();
  } catch (e) { toast(e.message, true); }
}

// ── ORDERS ────────────────────────────────────────────────
async function loadOrders() {
  try {
    const data = await apiFetch(ROUTES.orders);
    if (!data) return;
    const el = document.getElementById('orders-table-body');
    if (!el) return;
    if (!data.length) { el.innerHTML = '<tr><td colspan="6" class="empty">No orders yet.</td></tr>'; return; }
    el.innerHTML = data.map(o => `
      <tr>
        <td>#${o.id}</td>
        <td>${escHtml(o.customer_phone)}</td>
        <td>${escHtml(o.product_name)}</td>
        <td>${o.quantity}</td>
        <td>$${(o.total_price || 0).toFixed(2)}</td>
        <td><span class="badge ${o.status === 'pending' ? 'badge-yellow' : 'badge-green'}">${escHtml(o.status || 'pending')}</span></td>
      </tr>`).join('');
  } catch (e) { toast('Orders load failed: ' + e.message, true); }
}

// ── CONVERSATIONS ─────────────────────────────────────────
async function loadConversations() {
  const cl = document.getElementById('contact-list');
  if (!cl) return;
  try {
    cl.innerHTML = '<div class="empty">Loading…</div>';
    const data = await apiFetch(ROUTES.conversations);
    if (!data) return;
    const list = Array.isArray(data) ? data : (data.data || []);
    if (!list.length) { cl.innerHTML = '<div class="empty">No conversations yet.</div>'; return; }
    cl.innerHTML = list.map(c => {
      const lastAt = c.last_message_at || c.last_seen;
      return `
        <div class="contact-item" onclick="openChat('${escHtml(c.phone)}',this)">
          <div class="contact-avatar">👤</div>
          <div class="contact-info">
            <div class="contact-phone">${escHtml(c.phone || '—')}</div>
            <div class="contact-preview">${c.last_direction === 'outgoing' ? '🤖 ' : ''}${escHtml((c.last_message || '').slice(0, 60))}</div>
          </div>
          <div class="contact-meta">
            <div class="contact-time">${fmtTime(lastAt)}</div>
            ${(c.unread_count || 0) > 0 ? `<div class="unread-badge">${c.unread_count}</div>` : ''}
          </div>
        </div>`;
    }).join('');
  } catch (e) { if (cl) cl.innerHTML = `<div class="empty">⚠ ${e.message}</div>`; }
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
    <div class="chat-messages" id="chat-msgs"><div class="empty">Loading…</div></div>
    <div class="chat-reply-bar" id="chat-reply-bar">
      <textarea class="chat-reply-input" id="chat-reply-input"
        placeholder="Type a reply… (Enter to send, Shift+Enter for new line)"
        rows="1"
        onkeydown="handleDashboardSendKey(event)"
        oninput="autoResizeDashboard(this)"></textarea>
      <button class="chat-reply-btn" id="chat-reply-btn" onclick="sendFromDashboard().catch(()=>{})">Send ➤</button>
    </div>`;

  win.dataset.activePhone = phone;
  await loadChatMessages(phone);
}

async function loadChatMessages(phone) {
  const el = document.getElementById('chat-msgs');
  if (!el) return;
  try {
    const raw = await apiFetch(`${ROUTES.conversations}/${encodeURIComponent(phone)}`);
    if (!raw) return;
    const msgs = Array.isArray(raw) ? raw : (Array.isArray(raw.messages) ? raw.messages : []);
    if (!msgs.length) { el.innerHTML = '<div class="empty">No messages yet.</div>'; return; }
    el.innerHTML = msgs.map(m => {
      const text  = m.message || m.text || '';
      const dir   = m.direction || '';
      const isBc  = text.startsWith('[BROADCAST]');
      const isOut = dir === 'outgoing' || dir === 'out';
      const cls   = isBc ? 'msg-broadcast' : `msg-${isOut ? 'out' : 'in'}`;
      const txt   = isBc ? '📢 ' + escHtml(text.replace('[BROADCAST] ', '')) : escHtml(text);
      return `<div class="msg ${cls}">${txt}<div class="msg-time">${fmtTime(m.created_at)}</div></div>`;
    }).join('');
    el.scrollTop = el.scrollHeight;
  } catch (e) { if (el) el.innerHTML = `<div class="empty">⚠ ${e.message}</div>`; }
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
    // Look up customer_id from the CRM list
    const crmList  = await apiFetch('/chat/customers');
    const customers = Array.isArray(crmList) ? crmList : [];
    const customer  = customers.find(cu => cu.phone === phone);
    if (!customer) { toast('Customer not found — have they messaged you first?', true); return; }

    const res = await apiFetch('/chat/send', {
      method: 'POST',
      body:   JSON.stringify({ customer_id: customer.id, text }),
    });
    if (res && res.whatsapp_result && res.whatsapp_result.error) {
      toast(`⚠ Saved but WhatsApp delivery may have failed: ${res.whatsapp_result.error}`, true);
    } else {
      toast('✅ Sent');
    }
    await loadChatMessages(phone);
  } catch (e) {
    toast('Send failed: ' + e.message, true);
    if (input) input.value = text;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function handleDashboardSendKey(e) {
  if (e && e.key === 'Enter' && !e.shiftKey) {
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
async function loadCustomers() {
  try {
    const data = await apiFetch(ROUTES.customers);
    if (!data) return;
    customerPhones = Array.isArray(data.phones) ? data.phones.filter(Boolean) : [];
    const rc = document.getElementById('recipient-count');
    if (rc) rc.textContent = customerPhones.length
      ? `Will send to ${customerPhones.length} customer${customerPhones.length > 1 ? 's' : ''}`
      : 'No customers yet';
    const rl = document.getElementById('recipient-list');
    if (rl) rl.innerHTML = customerPhones.length
      ? `<div style="display:flex;flex-wrap:wrap;gap:6px;">${customerPhones.map(p =>
          `<span style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:4px 10px;font-family:var(--mono);font-size:11px;color:var(--text-dim);">📱 ${escHtml(p)}</span>`
        ).join('')}</div>`
      : '<div class="empty">No customers yet.</div>';
  } catch (e) { const rl = document.getElementById('recipient-list'); if (rl) rl.innerHTML = `<div class="empty">⚠ ${e.message}</div>`; }
}

function updatePreview() {
  const bm = document.getElementById('broadcast-msg');
  const pv = document.getElementById('preview-box');
  const cc = document.getElementById('char-count');
  const msg = bm ? bm.value : '';
  if (pv) pv.innerHTML = msg ? escHtml(msg) : '<span style="color:var(--text-dim);font-style:italic;">Message preview...</span>';
  if (cc) cc.textContent = `${msg.length} / 1024`;
}

function setTpl(t) {
  const bm = document.getElementById('broadcast-msg');
  if (bm) bm.value = t;
  updatePreview();
}

async function sendBroadcast() {
  const bm  = document.getElementById('broadcast-msg');
  const msg = (bm ? bm.value : '').trim();
  if (!msg) { toast('Write a message first', true); return; }
  if (!customerPhones.length) { toast('No customers to send to', true); return; }
  if (!confirm(`Send to ${customerPhones.length} customer(s)?`)) return;

  const btn    = document.getElementById('send-btn');
  const result = document.getElementById('broadcast-result');
  if (btn)    btn.disabled = true;
  if (result) result.style.display = 'none';

  try {
    const data = await apiFetch(ROUTES.broadcast, { method: 'POST', body: JSON.stringify({ message: msg }) });
    if (!data) return;
    if (result) {
      result.style.display = 'block';
      if (data.failed === 0) {
        result.className = 'broadcast-result success';
        result.textContent = `✅ Sent to ${data.sent} customer${data.sent > 1 ? 's' : ''}!`;
        toast(`📢 Broadcast sent to ${data.sent}!`);
        if (bm) bm.value = '';
        updatePreview();
      } else {
        result.className = 'broadcast-result error';
        result.textContent = `Sent: ${data.sent} | Failed: ${data.failed}${data.failed_numbers ? ' — ' + data.failed_numbers.join(', ') : ''}`;
      }
    }
  } catch (e) {
    if (result) { result.style.display = 'block'; result.className = 'broadcast-result error'; result.textContent = `❌ ${e.message}`; }
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── STATUS ────────────────────────────────────────────────
async function checkStatus() {
  const el   = document.getElementById('api-status-text');
  const spin = document.getElementById('api-spin');
  try {
    const r = await fetch(`${API}/`);
    if (r.ok) {
      if (el)   el.textContent = 'API Online';
      if (spin) spin.style.borderTopColor = 'var(--green)';
    } else {
      if (el)   el.textContent = 'API Error';
      if (spin) spin.style.borderTopColor = 'var(--red)';
    }
  } catch {
    if (el)   el.textContent = 'API Offline';
    if (spin) spin.style.borderTopColor = 'var(--red)';
  }
}

// ── SETTINGS ──────────────────────────────────────────────
async function loadSettings() {
  try {
    const b = await apiFetch('/me');
    if (b && b.whatsapp_phone_id) { const el = document.getElementById('set-phone-id'); if (el) el.value = b.whatsapp_phone_id; }
    if (b && b.name)              { const el = document.getElementById('set-biz-name');  if (el) el.value = b.name; }
  } catch {}
  const savedTheme = localStorage.getItem('wazi_theme') || 'dark';
  const savedFont  = localStorage.getItem('wazi_font')  || "'Syne',sans-serif";
  setTheme(savedTheme, true);
  const fontSel = document.getElementById('font-select');
  if (fontSel) { fontSel.value = savedFont; applyFont(savedFont, true); }
}

async function saveSettings() {
  const phoneId = (document.getElementById('set-phone-id') || {}).value?.trim();
  const tok     = (document.getElementById('set-token')    || {}).value?.trim();
  if (!phoneId) { toast('Enter Phone Number ID', true); return; }
  try {
    await apiFetch('/me', {
      method: 'PATCH',
      body:   JSON.stringify({ whatsapp_phone_id: phoneId, ...(tok ? { whatsapp_token: tok } : {}) }),
    });
    toast('✅ Credentials saved');
    const stok = document.getElementById('set-token'); if (stok) stok.value = '';
  } catch (e) { toast('Failed: ' + e.message, true); }
}

async function saveBusinessName() {
  const name = ((document.getElementById('set-biz-name') || {}).value || '').trim();
  if (!name) { toast('Enter a business name', true); return; }
  try {
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({ name }) });
    bizName = name;
    localStorage.setItem('wazi_biz', bizName);
    const rl  = document.getElementById('sidebar-role-label'); if (rl)  rl.textContent = bizName;
    const hdr = document.getElementById('biz-name-header');    if (hdr) hdr.textContent = '🟢 ' + bizName;
    toast('✅ Business name updated');
  } catch (e) { toast('Failed: ' + e.message, true); }
}

function setTheme(theme, silent = false) {
  document.body.classList.toggle('light', theme === 'light');
  localStorage.setItem('wazi_theme', theme);
  const db = document.getElementById('theme-dark-btn');
  const lb = document.getElementById('theme-light-btn');
  if (db) { db.style.color = theme === 'dark' ? 'var(--green)' : ''; db.style.borderColor = theme === 'dark' ? 'var(--green-dim)' : ''; }
  if (lb) { lb.style.color = theme === 'light' ? 'var(--green)' : ''; lb.style.borderColor = theme === 'light' ? 'var(--green-dim)' : ''; }
  if (!silent) toast(theme === 'light' ? '☀️ Light mode' : '🌙 Dark mode');
}

function applyFont(font, silent = false) {
  document.body.style.fontFamily = font;
  localStorage.setItem('wazi_font', font);
}

// ── UTILS ─────────────────────────────────────────────────
function fmtTime(iso) {
  if (!iso) return '—';
  try {
    let s = String(iso).trim().replace(' ', 'T');
    if (!/[Z+\-]\d*$/.test(s.slice(10))) s += 'Z';
    const d = new Date(s);
    if (isNaN(d.getTime())) return '—';
    const diff = Date.now() - d;
    if (diff < 60000 && diff >= 0) return 'just now';
    return d.toLocaleString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch { return '—'; }
}

function escHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

let _toastTimer;
function toast(msg, isError = false) {
  let t = document.getElementById('toast-notification');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast-notification';
    t.style.cssText = 'position:fixed;bottom:24px;right:24px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:12px 18px;font-size:13px;z-index:9999;transition:opacity .3s;';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.color       = isError ? 'var(--red)'   : 'var(--green)';
  t.style.borderColor = isError ? 'var(--red)'   : 'var(--green)';
  t.style.opacity     = '1';
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { if (t) t.style.opacity = '0'; }, 3200);
}

function copy(text) {
  navigator.clipboard.writeText(text);
  toast('Copied to clipboard ✅');
}

function setLoading(el, state = true) {
  if (!el) return;
  el.style.opacity       = state ? '0.5' : '1';
  el.style.pointerEvents = state ? 'none' : 'auto';
}

// ── INIT ──────────────────────────────────────────────────
function init() {
  const savedTheme = localStorage.getItem('wazi_theme') || 'dark';
  const savedFont  = localStorage.getItem('wazi_font');
  setTheme(savedTheme, true);
  if (savedFont) document.body.style.fontFamily = savedFont;

  buildSidebar();
  checkStatus();

  if (userRole === 'superadmin') {
    loadAdminData();
  } else {
    loadOrders();
    loadProducts();
    loadConversations();
  }
}

// Boot if already logged in
if (token && userRole) {
  const ls = document.getElementById('login-screen');
  if (ls) ls.style.display = 'none';
  init();
}

// Auto-refresh every 15 s
setInterval(() => {
  if (!token) return;
  if (userRole === 'superadmin') loadAdminData().catch(() => {});
  else                           loadOrders().catch(() => {});
  checkStatus().catch(() => {});
}, 15000);
