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
      <button class="nav-item" onclick="showSection('crm',this);closeSidebar()"><span class="icon">👥</span> Customers <span id="nav-crm-badge" class="nav-badge" style="display:none"></span></button>
      <button class="nav-item" onclick="showSection('reminders',this);closeSidebar()"><span class="icon">⏳</span> Reminders <span id="nav-rem-badge" class="nav-badge nav-badge-amber" style="display:none"></span></button>
      <button class="nav-item" onclick="showSection('conversations',this);closeSidebar()"><span class="icon">💬</span> Conversations</button>
      <button class="nav-item" onclick="window.open('/inbox','_blank');closeSidebar()"><span class="icon">📥</span> Live Inbox</button>
      <button class="nav-item" onclick="showSection('broadcast',this);closeSidebar()"><span class="icon">📢</span> Campaigns</button>
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
  if (name==='broadcast') { loadCustomers(); loadCampaignAudiences(); }
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
    _setVal('set-category',      b.category || '');
    _setVal('set-description',   b.description || '');
    _setVal('set-contact-phone', b.contact_phone || '');
    _setVal('set-support-email', b.support_email || '');
    _setVal('set-address',       b.address || '');
    _setVal('set-city',          b.city || '');
    _setVal('set-hours',         b.business_hours || '');
    _setVal('set-instagram',     b.instagram || '');
    _setVal('set-facebook',      b.facebook || '');
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

  // Appearance
  const savedTheme = localStorage.getItem('wazi_theme') || 'dark';
  const savedFont  = localStorage.getItem('wazi_font') || "'Syne',sans-serif";
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
    await apiFetch('/me', { method: 'PATCH', body: JSON.stringify({
      name,
      category:       _getVal('set-category'),
      description:    _getVal('set-description'),
      contact_phone:  _getVal('set-contact-phone'),
      support_email:  _getVal('set-support-email'),
      address:        _getVal('set-address'),
      city:           _getVal('set-city'),
      business_hours: _getVal('set-hours'),
      instagram:      _getVal('set-instagram'),
      facebook:       _getVal('set-facebook'),
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

async function savePaymentOptions() {
  toast('✅ Payment options saved');
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

// ════════════════════════════════════════════════════════════════════════════
// PHASE 1 — CRM SEGMENT CARD (overview) + REMINDERS BADGE
// ════════════════════════════════════════════════════════════════════════════

async function loadOverviewExtras() {
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
  } catch (_) {}
}

// Hook into the overview load
const _origLoadOrders = loadOrders;
async function loadOrders() {
  await _origLoadOrders();
  loadOverviewExtras();
}

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
    if (!data.length) {
      tbody.innerHTML = '<tr><td colspan="6"><div class="empty">No customers yet.</div></td></tr>';
      return;
    }
    tbody.innerHTML = data.map(c => {
      const seg = getSegmentLabel(c.order_count || 0, c.total_spent || 0);
      return `<tr>
        <td style="font-family:var(--mono);font-size:12px;">${escHtml(c.phone || '—')}</td>
        <td>${escHtml(c.customer_name || '—')}</td>
        <td>${c.order_count || 0}</td>
        <td style="color:var(--green);">$${parseFloat(c.total_spent || 0).toFixed(2)}</td>
        <td style="font-family:var(--mono);font-size:11px;color:var(--text-dim);">${c.last_seen ? fmtTime(c.last_seen) : '—'}</td>
        <td><button class="btn btn-ghost" style="font-size:11px;padding:3px 8px;" onclick="openCustomerDrawer(${JSON.stringify(c)})">View</button></td>
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
  document.getElementById('drawer-spent').textContent    = '$' + parseFloat(customer.total_spent||0).toFixed(2);
  document.getElementById('drawer-last').textContent     = customer.last_seen ? fmtTime(customer.last_seen) : '—';
  document.getElementById('drawer-orders-list').innerHTML = '<div style="color:var(--text-dim);font-family:var(--mono);font-size:11px;">Loading orders…</div>';
  document.getElementById('customer-drawer').classList.add('open');
  document.getElementById('drawer-overlay').classList.add('open');
  loadDrawerOrders(phone);
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
        <span style="color:var(--green);">$${parseFloat(o.total_price||0).toFixed(2)}</span>
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
        <td><span class="badge badge-green">$${parseFloat(o.total_price||0).toFixed(2)}</span></td>
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
          <div class="kanban-card-total">$${parseFloat(o.total_price||0).toFixed(2)}</div>
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
    await apiFetch(`/orders/${orderId}/status`, { method: 'PATCH', body: JSON.stringify({ status: pick }) });
    toast(`✅ ORDER-${orderId} → ${pick}`);
    await loadOrders();
  } catch (e) { toast('Update failed: ' + e.message, true); }
}

// Patch loadOrders to capture data for kanban + filter
const _origLoadOrdersPhase5 = loadOrders;
loadOrders = async function() {
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
    if (statR) statR.textContent = '$' + _ordersData.reduce((s,o)=>s+(o.total_price||0),0).toFixed(2);
    if (_ordersView === 'kanban') renderKanban(_ordersData);
    loadOverviewExtras();
  } catch(e) {
    ['orders-body','recent-orders-body'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.innerHTML=`<tr><td colspan="7"><div class="empty">⚠ ${e.message}</div></td></tr>`;
    });
  }
};


// ════════════════════════════════════════════════════════════════════════════
// PHASE 6 — ANALYTICS CHARTS
// ════════════════════════════════════════════════════════════════════════════

async function loadAnalyticsCharts() {
  try {
    const [stats, topCust] = await Promise.all([
      apiFetch(ROUTES.analyticsStats),
      apiFetch(ROUTES.analyticsTop + '?limit=5'),
    ]);

    // Update stat cards if present
    if (stats) {
      const map = {
        'stat-orders':    stats.total_orders,
        'stat-revenue':   stats.total_revenue != null ? '$' + parseFloat(stats.total_revenue).toFixed(2) : null,
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
  } catch (_) {}
}

// Hook analytics load into overview
document.addEventListener('DOMContentLoaded', () => {
  setTimeout(loadAnalyticsCharts, 500);
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
  };
  if (lbl) lbl.textContent = map[val] || val;
  updatePreview();
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
  };

  if (!confirm(`Send to ${audienceLabels[audience] || audience}?`)) return;

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
    // reset audience to all
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

// Also load on initial page load if we're on overview
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    setTimeout(loadGrowthInsights, 2000); // after initial data loads
  });
} else {
  setTimeout(loadGrowthInsights, 2000);
}
