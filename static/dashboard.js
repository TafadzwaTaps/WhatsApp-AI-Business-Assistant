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
};

let token       = localStorage.getItem('wazi_token');
let refreshTok  = localStorage.getItem('wazi_refresh');
let userRole    = localStorage.getItem('wazi_role');
let userName    = localStorage.getItem('wazi_user');
let bizName     = localStorage.getItem('wazi_biz');
let bizId       = parseInt(localStorage.getItem('wazi_business_id') || '0', 10);
let activePhone = null;
let customerPhones = [];

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

// ── AUTH ──────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.login-tab').forEach((t,i) => t.classList.toggle('active', (i===0&&tab==='login')||(i===1&&tab==='register')));
  document.getElementById('tab-login').classList.toggle('active', tab==='login');
  document.getElementById('tab-register').classList.toggle('active', tab==='register');
  const _lerr=document.getElementById('login-error'); if(_lerr) _lerr.textContent='';
}

async function doLogin() {
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  const errEl = document.getElementById('login-error');
  errEl.textContent = '';
  if (!username || !password) { errEl.textContent = 'Enter credentials'; return; }
  try {
    const res = await fetch(API + ROUTES.login, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });
    if (!res.ok) { const d=await res.json(); errEl.textContent = d.detail || 'Login failed'; return; }
    const data = await res.json();
    saveSession(data, username);
    const _ls=document.getElementById('login-screen'); if(_ls) _ls.style.display='none';
    // If we're on the landing page or signup, redirect to dashboard
    if (window.location.pathname !== '/dashboard') {
      window.location.href = '/dashboard';
      return;
    }
    init();
  } catch(e) { errEl.textContent = 'Cannot reach server. Is backend running?'; }
}

async function doRegister() {
  const errEl = document.getElementById('login-error');
  errEl.textContent = '';
  const payload = {
    business_name:    document.getElementById('reg-bizname').value.trim(),
    username:         document.getElementById('reg-username').value.trim(),
    password:         document.getElementById('reg-password').value,
    confirm_password: document.getElementById('reg-confirm').value,
    whatsapp_phone_id: document.getElementById('reg-phoneid').value.trim() || null,
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
}

async function tryRefresh() {
  if (!refreshTok) { logout(); return false; }
  try {
    const res = await fetch(API + ROUTES.refresh, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshTok })
    });
    if (!res.ok) { logout(); return false; }
    const data = await res.json();
    saveSession(data, userName);
    return true;
  } catch { logout(); return false; }
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

// ── API ───────────────────────────────────────────────────
async function apiFetch(path, opts={}, _retried=false) {
  // FIX: _retried flag prevents infinite refresh loops — one retry max.
  // If the refreshed token also gets a 401, logout() is called once.
  try {
    const res = await fetch(API + path, {
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      ...opts
    });
    if (res.status === 401 && !_retried) {
      const refreshed = await tryRefresh();
      if (refreshed) return apiFetch(path, opts, true);  // one retry only
      return null;  // tryRefresh already called logout()
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
      <button class="nav-item" onclick="showSection('conversations',this);closeSidebar()"><span class="icon">💬</span> Conversations</button>
      <button class="nav-item" onclick="window.open('/inbox','_blank');closeSidebar()"><span class="icon">📥</span> Live Inbox</button>
      <button class="nav-item" onclick="showSection('broadcast',this);closeSidebar()"><span class="icon">📢</span> Broadcast</button>
      <button class="nav-item" onclick="showSection('settings',this);closeSidebar()"><span class="icon">⚙️</span> Settings</button>`;
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
  if (name==='broadcast') loadCustomers();
  if (name==='settings') loadSettings();
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
    _s('sa-revenue', '$' + (stats.total_revenue||0).toFixed(2));
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
  const name=document.getElementById('b-name').value.trim();
  const username=document.getElementById('b-username').value.trim();
  const password=document.getElementById('b-password').value.trim();
  const phoneId=document.getElementById('b-phone-id').value.trim();
  const btoken=document.getElementById('b-token').value.trim();
  if (!name||!username||!password) { toast('Name, username and password required',true); return; }
  try {
    await apiFetch(ROUTES.adminBiz, {method:'POST', body:JSON.stringify({name, owner_username:username, owner_password:password, whatsapp_phone_id:phoneId||null, whatsapp_token:btoken||null})});
    toast(`✅ ${name} created`);
    closeModal();
    ['b-name','b-username','b-password','b-phone-id','b-token'].forEach(id=>{const el=document.getElementById(id);if(el)el.value='';});
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
    if (statR) statR.textContent = '$' + orders.reduce((s,o)=>s+(o.total_price||0),0).toFixed(2);
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
    <td><span class="badge badge-green">$${(o.total_price||0).toFixed(2)}</span></td>
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

async function loadProducts() {
  try {
    const raw = await apiFetch(ROUTES.products);
    if (!raw) return;
    const products = Array.isArray(raw) ? raw : (Array.isArray(raw.data) ? raw.data : []);
    const statP = document.getElementById('stat-products');
    if (statP) statP.textContent = products.length;

    const tbody = document.getElementById('products-body');
    if (tbody) {
      if (!products.length) {
        tbody.innerHTML=`<tr><td colspan="4"><div class="empty">No products yet. Add your first item above!</div></td></tr>`;
      } else {
        tbody.innerHTML = products.map(p => {
          const thumb = p.image_url
            ? `<img class="product-thumb" src="${escHtml(p.image_url)}" alt="${escHtml(p.name||'')}" onerror="this.style.display='none';this.nextSibling.style.display='flex'">`
              + `<div class="product-thumb-placeholder" style="display:none">📦</div>`
            : `<div class="product-thumb-placeholder">📦</div>`;
          return `<tr>
            <td style="width:52px">${thumb}</td>
            <td><strong>${escHtml(p.name||'—')}</strong></td>
            <td><span class="badge badge-green">$${(p.price||0).toFixed(2)}</span></td>
            <td><button class="btn btn-ghost" onclick="deleteProduct(${p.id})">✕</button></td>
          </tr>`;
        }).join('');
      }
    }

    const grid = document.getElementById('products-grid');
    if (grid) {
      if (!products.length) {
        grid.innerHTML = '<div class="empty" style="grid-column:1/-1">No products yet.</div>';
      } else {
        grid.innerHTML = products.map(p => `
          <div class="product-card">
            <div class="product-card-img">
              ${p.image_url
                ? `<img src="${escHtml(p.image_url)}" alt="${escHtml(p.name||'')}" onerror="this.parentNode.textContent='📦'">`
                : '📦'}
            </div>
            <div class="product-card-body">
              <div class="product-card-name">${escHtml(p.name||'—')}</div>
              <div class="product-card-price">$${(p.price||0).toFixed(2)}</div>
              <button class="btn btn-ghost" style="margin-top:8px;width:100%;font-size:11px;" onclick="deleteProduct(${p.id})">✕ Remove</button>
            </div>
          </div>`).join('');
      }
    }
  } catch(e) {
    const tbody = document.getElementById('products-body');
    if (tbody) tbody.innerHTML=`<tr><td colspan="4"><div class="empty">⚠ ${e.message}</div></td></tr>`;
  }
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

function _loadImageFile(file) {
  if (!file) return;
  if (!file.type.startsWith('image/')) { toast('Please select an image file', true); return; }
  if (file.size > 2 * 1024 * 1024) { toast('Image must be under 2MB', true); return; }
  const reader = new FileReader();
  reader.onload = (e) => {
    _pendingImgDataUrl = e.target.result;
    const preview = document.getElementById('img-preview');
    const clearBtn = document.getElementById('img-clear-btn');
    const area = document.getElementById('img-upload-area');
    if (preview) { preview.src = _pendingImgDataUrl; preview.classList.add('show'); }
    if (clearBtn) clearBtn.style.display = 'inline';
    if (area) area.style.display = 'none';
  };
  reader.readAsDataURL(file);
}

function clearProductImg() {
  _pendingImgDataUrl = null;
  const preview = document.getElementById('img-preview');
  const clearBtn = document.getElementById('img-clear-btn');
  const area = document.getElementById('img-upload-area');
  const fileInput = document.getElementById('product-img-file');
  if (preview) { preview.src = ''; preview.classList.remove('show'); }
  if (clearBtn) clearBtn.style.display = 'none';
  if (area) area.style.display = '';
  if (fileInput) fileInput.value = '';
}

async function addProduct() {
  const nameEl  = document.getElementById('product-name');
  const priceEl = document.getElementById('product-price');
  const name  = nameEl  ? nameEl.value.trim()  : '';
  const price = priceEl ? parseFloat(priceEl.value) : NaN;
  if (!name || isNaN(price) || price <= 0) { toast('Enter a valid name and price', true); return; }
  try {
    const payload = { name, price };
    if (_pendingImgDataUrl) payload.image_url = _pendingImgDataUrl;
    await apiFetch(ROUTES.products, { method: 'POST', body: JSON.stringify(payload) });
    if (nameEl)  nameEl.value  = '';
    if (priceEl) priceEl.value = '';
    clearProductImg();
    toast(`✅ ${name} added`);
    loadProducts();
  } catch(e) { toast(e.message || 'Failed to add product', true); }
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
    const _sc=document.getElementById('stat-customers'); if(_sc) _sc.textContent=list_data.length;
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
async function loadCustomers(){
  try{
    const data=await apiFetch(ROUTES.customers);
    if(!data)return;
    customerPhones = Array.isArray(data.phones) ? data.phones.filter(Boolean)
      : Array.isArray(data) ? data.filter(Boolean) : [];
    const _rc=document.getElementById('recipient-count');
    if(_rc) _rc.textContent=customerPhones.length?`Will send to ${customerPhones.length} customer${customerPhones.length>1?'s':''}`:'No customers yet';
    const _rl=document.getElementById('recipient-list');
    if(_rl) _rl.innerHTML=customerPhones.length?`<div style="display:flex;flex-wrap:wrap;gap:6px;">${customerPhones.map(p=>`<span style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:4px 10px;font-family:var(--mono);font-size:11px;color:var(--text-dim);">📱 ${escHtml(p)}</span>`).join('')}</div>`:'<div class="empty">No customers yet.</div>';
  } catch(e){ const _rle=document.getElementById('recipient-list'); if(_rle) _rle.innerHTML=`<div class="empty">⚠ ${e.message}</div>`; }
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

async function sendBroadcast(){
  const _bm=document.getElementById('broadcast-msg');
  const msg=(_bm?_bm.value:'').trim();
  if(!msg){toast('Write a message first',true);return;}
  if(!customerPhones.length){toast('No customers to send to',true);return;}
  if(!confirm(`Send to ${customerPhones.length} customer(s)?`))return;
  const btn=document.getElementById('send-btn');
  const result=document.getElementById('broadcast-result');
  if(btn) btn.disabled=true;
  if(result) result.style.display='none';
  try{
    const data=await apiFetch(ROUTES.broadcast,{method:'POST',body:JSON.stringify({message:msg})});
    if(!data) return;
    if(result){
      result.style.display='block';
      if(data.failed===0){
        result.className='broadcast-result success';
        result.textContent=`✅ Sent to ${data.sent} customer${data.sent>1?'s':''}!`;
        toast(`📢 Broadcast sent to ${data.sent}!`);
        if(_bm) _bm.value='';
        updatePreview();
      } else {
        result.className='broadcast-result error';
        result.textContent=`Sent: ${data.sent} | Failed: ${data.failed}`;
      }
    }
  } catch(e){
    if(result){result.style.display='block';result.className='broadcast-result error';result.textContent=`❌ ${e.message}`;}
    toast(e.message,true);
  } finally {
    if(btn) btn.disabled=false;
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
  try {
    const b = await apiFetch('/me');
    if(b&&b.whatsapp_phone_id){const _spi=document.getElementById('set-phone-id');if(_spi)_spi.value=b.whatsapp_phone_id;}
    if(b&&b.name){const _sbn=document.getElementById('set-biz-name');if(_sbn)_sbn.value=b.name;}
  } catch(e) {}
  const savedTheme = localStorage.getItem('wazi_theme') || 'dark';
  const savedFont = localStorage.getItem('wazi_font') || "'Syne',sans-serif";
  setTheme(savedTheme, true);
  const fontSel = document.getElementById('font-select');
  if (fontSel) { fontSel.value = savedFont; applyFont(savedFont, true); }
}

async function saveSettings() {
  const phoneId = document.getElementById('set-phone-id').value.trim();
  const token2 = document.getElementById('set-token').value.trim();
  if (!phoneId) { toast('Enter Phone Number ID', true); return; }
  try {
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({ whatsapp_phone_id: phoneId, whatsapp_token: token2 || undefined }) });
    toast('✅ Credentials saved');
    const _stok=document.getElementById('set-token'); if(_stok) _stok.value='';
  } catch(e) { toast('Failed: ' + e.message, true); }
}

async function saveBusinessName() {
  const name = (document.getElementById('set-biz-name').value || '').trim();
  if (!name) { toast('Enter a business name', true); return; }
  try {
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({ name }) });
    bizName = name;
    localStorage.setItem('wazi_biz', bizName);
    const _srl=document.getElementById('sidebar-role-label'); if(_srl) _srl.textContent=bizName;
    const hdr = document.getElementById('biz-name-header');
    if (hdr) hdr.textContent = '🟢 ' + bizName;
    toast('✅ Business name updated');
  } catch(e) { toast('Failed: ' + e.message, true); }
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
}

// ── UTILS ─────────────────────────────────────────────────
function fmtTime(iso){
  if(!iso) return '—';
  try {
    let s = String(iso).trim();
    s = s.replace(' ', 'T');
    if(!/[Z+\-]\d*$/.test(s.slice(10))) s += 'Z';
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

// ── INIT ──────────────────────────────────────────────────
function init(){
  const savedTheme = localStorage.getItem('wazi_theme') || 'dark';
  const savedFont = localStorage.getItem('wazi_font');
  setTheme(savedTheme, true);
  if (savedFont) document.body.style.fontFamily = savedFont;

  buildSidebar();
  checkStatus();
  if(userRole==='superadmin'){loadAdminData();}
  else{loadOrders();loadProducts();loadConversations();}
}

if(token&&userRole){
  const _ls4=document.getElementById('login-screen');
  if(_ls4) _ls4.style.display='none';
  init();
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
}, 15000);

function setLoading(el, state=true) {
  if (!el) return;
  el.style.opacity = state ? "0.5" : "1";
  el.style.pointerEvents = state ? "none" : "auto";
}
