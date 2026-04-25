/* ═══════════════════════════════════════════════════════════
   WaziBot Inbox — inbox.js
   All original functionality preserved + mobile/theme enhancements
   ═══════════════════════════════════════════════════════════ */

'use strict';

/* ── CONFIG & SESSION ───────────────────────────────────── */
const API = 'https://wazibot-api-assistant.onrender.com';

// Support both key names (dashboard saves as wazi_token / wazi_business_id)
const token = localStorage.getItem('wazi_token') || localStorage.getItem('wazibot_token');
const bizId = parseInt(
  localStorage.getItem('wazi_business_id') ||
  localStorage.getItem('wazibot_biz_id') || '0'
);

if (!token) { window.location.href = '/dashboard'; }

/* ── STATE ──────────────────────────────────────────────── */
let allConversations  = [];
let currentCustomerId = null;
let currentPhone      = null;
let msgOffset         = 0;
const msgLimit        = 50;
let hasMoreMessages   = false;
let wsConn            = null;
let activeFilter      = 'all';
let sidebarOpen       = false;

/* ── THEME ──────────────────────────────────────────────── */
function applyTheme(theme) {
  document.body.classList.toggle('light', theme === 'light');
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = theme === 'light' ? '🌙' : '☀️';
  localStorage.setItem('wazi_inbox_theme', theme);
}

function toggleTheme() {
  const current = document.body.classList.contains('light') ? 'light' : 'dark';
  applyTheme(current === 'light' ? 'dark' : 'light');
}

function initTheme() {
  const saved = localStorage.getItem('wazi_inbox_theme') ||
                (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  applyTheme(saved);
}

/* ── MOBILE SIDEBAR ─────────────────────────────────────── */
function openSidebar() {
  sidebarOpen = true;
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  if (sidebar) sidebar.classList.add('open');
  if (overlay) overlay.classList.add('open');
}

function closeSidebar() {
  sidebarOpen = false;
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  if (sidebar) sidebar.classList.remove('open');
  if (overlay) overlay.classList.remove('open');
}

function toggleSidebar() {
  sidebarOpen ? closeSidebar() : openSidebar();
}

/* ── API HELPER ─────────────────────────────────────────── */
async function apiFetch(path, opts = {}) {
  try {
    const res = await fetch(API + path, {
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      ...opts,
    });
    if (res.status === 401) {
      window.location.href = '/dashboard';
      throw new Error('Unauthorized');
    }
    if (!res.ok) {
      let msg = res.statusText || 'API error';
      try { const e = await res.json(); msg = e.detail || msg; } catch {}
      throw new Error(msg);
    }
    return res.json();
  } catch (err) {
    if (err.message === 'Unauthorized') throw err;
    if (err instanceof TypeError) throw new Error('Cannot reach server — is the backend running?');
    throw err;
  }
}

/* ── WEBSOCKET ──────────────────────────────────────────── */
function connectWS() {
  if (!bizId) return;

  const wsUrl = `wss://wazibot-api-assistant.onrender.com/ws/chat/${bizId}?token=${encodeURIComponent(token)}`;
  wsConn = new WebSocket(wsUrl);

  wsConn.onopen = () => {
    setWsStatus('live', 'Live');
    // Heartbeat every 25 s
    const hb = setInterval(() => {
      if (wsConn && wsConn.readyState === WebSocket.OPEN) {
        wsConn.send(JSON.stringify({ type: 'ping' }));
      } else {
        clearInterval(hb);
      }
    }, 25000);
  };

  wsConn.onclose = () => {
    setWsStatus('dead', 'Reconnecting…');
    setTimeout(connectWS, 3000);
  };

  wsConn.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload.type === 'pong') return;

      if (payload.event === 'new_message') {
        const { customer_id, message } = payload;

        // If this conversation is currently open, append bubble
        if (customer_id === currentCustomerId && message) {
          appendBubble(message);
          scrollToBottom();
          if (message.direction === 'incoming') markRead().catch(() => {});
        }

        // Always refresh sidebar to update preview + unread badge
        loadConversations(false).catch(() => {});
      }
    } catch (err) {
      console.warn('WS parse error:', err);
    }
  };

  wsConn.onerror = () => wsConn.close();
}

function setWsStatus(cls, label) {
  const dot = document.getElementById('ws-dot');
  const lbl = document.getElementById('ws-label');
  if (dot) dot.className = 'ws-dot ' + cls;
  if (lbl) lbl.textContent = label;
}

/* ── CONVERSATIONS SIDEBAR ──────────────────────────────── */
async function loadConversations(showSkeleton = true) {
  try {
    if (showSkeleton) showSkeletons();
    const unread = activeFilter === 'unread';
    const raw = await apiFetch(`/chat/conversations?unread_only=${unread}`);
    allConversations = Array.isArray(raw) ? raw
      : (raw && Array.isArray(raw.data)) ? raw.data : [];
    renderContacts(allConversations);
  } catch (e) {
    const list = document.getElementById('contact-list');
    if (list) {
      list.innerHTML = `<div class="empty-state" style="color:var(--red)">⚠ ${escHtml(e.message)}</div>`;
    }
  }
}

function showSkeletons() {
  const list = document.getElementById('contact-list');
  if (!list) return;
  list.innerHTML = `
    <div class="skeleton-list">
      ${[1,2,3,4].map(() => `
        <div class="skeleton-item">
          <div class="skeleton skeleton-avatar"></div>
          <div class="skeleton-lines">
            <div class="skeleton skeleton-line-a"></div>
            <div class="skeleton skeleton-line-b"></div>
          </div>
        </div>`).join('')}
    </div>`;
}

function renderContacts(convos) {
  const searchEl = document.getElementById('search-input');
  const list = document.getElementById('contact-list');
  if (!list) return;

  const search = searchEl ? searchEl.value.trim().toLowerCase() : '';
  const safe   = Array.isArray(convos) ? convos : [];

  let filtered = safe;
  if (search) filtered = safe.filter(c => (c.phone || '').includes(search));
  if (activeFilter === 'recent') filtered = filtered.slice(0, 20);

  if (!filtered.length) {
    list.innerHTML = `<div class="empty-state">${search ? 'No results for "' + escHtml(search) + '"' : 'No conversations yet'}</div>`;
    return;
  }

  // Build via fragment — avoids full innerHTML reparse
  const frag = document.createDocumentFragment();
  filtered.forEach(c => {
    const div = document.createElement('div');
    div.className = 'contact-item' + (c.customer_id === currentCustomerId ? ' active' : '');
    div.dataset.customerId = c.customer_id;
    div.onclick = () => openChat(c.customer_id, c.phone || '', c.last_seen || '');
    div.innerHTML = `
      <div class="contact-avatar">👤</div>
      <div class="contact-info">
        <div class="contact-phone">${escHtml(c.phone || '—')}</div>
        <div class="contact-preview">${c.last_direction === 'outgoing' ? '🤖 ' : ''}${escHtml(safeText(c.last_message))}</div>
      </div>
      <div class="contact-meta">
        <div class="contact-time">${formatTime(c.last_message_at)}</div>
        ${(c.unread_count || 0) > 0 ? `<div class="unread-badge">${c.unread_count}</div>` : ''}
      </div>`;
    frag.appendChild(div);
  });

  list.innerHTML = '';
  list.appendChild(frag);
}

function filterContacts(val) { renderContacts(allConversations); }

function setFilter(f) {
  activeFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  const fb = document.getElementById('filter-' + f);
  if (fb) fb.classList.add('active');
  loadConversations(false).catch(() => {});
}

/* ── OPEN CHAT ──────────────────────────────────────────── */
async function openChat(customerId, phone, lastSeen) {
  if (!customerId || !phone) return;

  currentCustomerId = customerId;
  currentPhone      = phone;
  msgOffset         = 0;

  // On mobile: close sidebar when opening a chat
  if (window.innerWidth < 900) closeSidebar();

  // Show active-chat panel
  const noSel = document.getElementById('no-selection');
  if (noSel) noSel.style.display = 'none';

  const ac = document.getElementById('active-chat');
  if (!ac) return;
  ac.style.display    = 'flex';
  ac.style.flexDirection = 'column';
  ac.style.overflow   = 'hidden';
  ac.style.flex       = '1';

  const phoneEl  = document.getElementById('chat-phone');
  if (phoneEl)  phoneEl.textContent = phone;

  const statusEl = document.getElementById('chat-status');
  if (statusEl) statusEl.textContent = lastSeen ? `Last seen ${formatTime(lastSeen)}` : 'Customer';

  // Highlight active contact in sidebar
  document.querySelectorAll('.contact-item').forEach(el => {
    el.classList.toggle('active', parseInt(el.dataset.customerId) === customerId);
  });

  // Clear + load messages
  const msgContainer = document.getElementById('chat-messages');
  if (msgContainer) {
    msgContainer.innerHTML =
      '<div style="text-align:center;padding:28px;font-family:var(--mono);font-size:11px;color:var(--text-dim)">Loading…</div>';
  }

  const lmBtn = document.getElementById('load-more-btn');
  if (lmBtn) lmBtn.style.display = 'none';

  await loadMessages(customerId, true);
  await markRead();
}

/* ── LOAD MESSAGES ──────────────────────────────────────── */
async function loadMessages(customerId, reset = false) {
  if (reset) msgOffset = 0;
  try {
    const data = await apiFetch(`/chat/messages/${customerId}?limit=${msgLimit}&offset=${msgOffset}`);
    const msgs = (data && Array.isArray(data.messages)) ? data.messages : [];

    const container = document.getElementById('chat-messages');
    if (!container) return;

    const lmBtn = document.getElementById('load-more-btn');

    if (reset) {
      if (lmBtn && lmBtn.parentNode === container) container.removeChild(lmBtn);
      container.innerHTML = '';
      if (lmBtn) {
        lmBtn.style.display = 'none';
        container.appendChild(lmBtn);
      }
    }

    if (msgs.length === 0 && reset) {
      container.innerHTML =
        '<div style="text-align:center;padding:48px 20px;font-family:var(--mono);font-size:12px;color:var(--text-dim)">No messages yet — say hello! 👋</div>';
      return;
    }

    const frag = document.createDocumentFragment();
    msgs.forEach(m => frag.appendChild(createBubble(m)));

    if (reset) {
      container.appendChild(frag);
    } else {
      const anchor = lmBtn && lmBtn.nextSibling ? lmBtn.nextSibling : (container.children[1] || null);
      container.insertBefore(frag, anchor);
    }

    hasMoreMessages = msgs.length === msgLimit;
    if (lmBtn) lmBtn.style.display = hasMoreMessages ? 'block' : 'none';

    if (reset) scrollToBottom();

  } catch (e) {
    const container = document.getElementById('chat-messages');
    if (container) {
      container.innerHTML =
        `<div style="text-align:center;padding:20px;font-family:var(--mono);font-size:12px;color:var(--red)">⚠ ${escHtml(e.message)}</div>`;
    }
  }
}

async function loadMoreMessages() {
  if (!currentCustomerId) return;
  msgOffset += msgLimit;
  await loadMessages(currentCustomerId, false);
}

/* ── BUBBLES ────────────────────────────────────────────── */
function createBubble(msg) {
  const text = safeText(msg.text);
  const rawDir = (msg.direction || '');
  const isBroadcast = text.startsWith('[BROADCAST]');
  const dir = rawDir.startsWith('in') ? 'incoming' : 'outgoing';

  const div = document.createElement('div');
  div.className = `msg ${dir}${isBroadcast ? ' broadcast' : ''}`;
  div.dataset.msgId = msg.id || '';

  const displayText = isBroadcast ? '📢 ' + escHtml(text.replace('[BROADCAST] ', '')) : escHtml(text);

  div.innerHTML = `
    <div class="bubble">${displayText}</div>
    <div class="msg-meta">
      <span class="msg-time">${formatTime(msg.created_at)}</span>
      ${dir === 'outgoing' ? `<span class="msg-status ${msg.status || 'sent'}"></span>` : ''}
    </div>`;
  return div;
}

function appendBubble(msg) {
  const container = document.getElementById('chat-messages');
  if (!container) return;
  container.appendChild(createBubble(msg));
}

/* ── SEND MESSAGE ───────────────────────────────────────── */
async function sendMessage() {
  if (!currentCustomerId) return;
  const input   = document.getElementById('send-input');
  const sendBtn = document.getElementById('send-btn');
  if (!input || !sendBtn) return;

  const text = input.value.trim();
  if (!text) return;

  input.value = '';
  input.style.height = 'auto';
  sendBtn.disabled = true;

  // Optimistic render for snappy UX
  appendBubble({
    id: null,
    text,
    direction: 'outgoing',
    status: 'sent',
    created_at: new Date().toISOString(),
  });
  scrollToBottom();
  showTyping(900);

  try {
    const res = await apiFetch('/chat/send', {
      method: 'POST',
      body: JSON.stringify({ customer_id: currentCustomerId, text }),
    });
    if (res && res.whatsapp_result && res.whatsapp_result.error) {
      showToast('⚠ Saved but WhatsApp delivery may have failed', true);
    }
  } catch (e) {
    showToast('Send failed: ' + e.message, true);
  } finally {
    if (sendBtn) sendBtn.disabled = false;
    if (input) input.focus();
  }
}

function handleSendKey(e) {
  if (!e) return;
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage().catch(() => {});
  }
}

function autoResize(el) {
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

/* ── READ TRACKING ──────────────────────────────────────── */
async function markRead() {
  if (!currentCustomerId) return;
  try {
    await apiFetch(`/chat/read/${currentCustomerId}`, { method: 'POST' });
    const conv = allConversations.find(c => c.customer_id === currentCustomerId);
    if (conv) {
      conv.unread_count = 0;
      renderContacts(allConversations);
    }
  } catch (_) { /* silent — read tracking is non-critical */ }
}

/* ── TYPING INDICATOR ───────────────────────────────────── */
function showTyping(ms) {
  const el = document.getElementById('typing-indicator');
  if (!el) return;
  el.classList.add('visible');
  scrollToBottom();
  setTimeout(() => { if (el) el.classList.remove('visible'); }, ms);
}

/* ── SCROLL ─────────────────────────────────────────────── */
function scrollToBottom() {
  const c = document.getElementById('chat-messages');
  if (!c) return;
  requestAnimationFrame(() => { if (c) c.scrollTop = c.scrollHeight; });
}

/* ── UTILS ──────────────────────────────────────────────── */
function safeText(val) {
  if (val === null || val === undefined) return '';
  return String(val);
}

function escHtml(s) {
  return safeText(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function formatTime(iso) {
  if (!iso) return '—';
  try {
    let s = String(iso).trim().replace(' ', 'T');
    if (!/[Z+][0-9]*$/.test(s.slice(10))) s += 'Z';
    const d = new Date(s);
    if (isNaN(d.getTime())) return '—';
    const now  = new Date();
    const diff = now - d;
    if (diff < 0)       return d.toLocaleString();
    if (diff < 60000)   return 'just now';
    if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
    if (diff < 86400000) return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
  } catch { return '—'; }
}

/* ── TOAST ──────────────────────────────────────────────── */
let toastTimer;

function showToast(msg, isError = false) {
  let t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    t.className = 'toast hidden';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.borderLeftColor = isError ? 'var(--red)' : 'var(--green)';
  t.style.color = isError ? 'var(--red)' : 'var(--green)';
  t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { if (t) t.classList.add('hidden'); }, 3200);
}

/* ── INIT ───────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  initTheme();

  // Theme toggle button
  const themeBtn = document.getElementById('theme-toggle');
  if (themeBtn) themeBtn.addEventListener('click', toggleTheme);

  // Hamburger
  const hamburger = document.getElementById('hamburger');
  if (hamburger) hamburger.addEventListener('click', toggleSidebar);

  // Sidebar overlay click-to-close
  const overlay = document.getElementById('sidebar-overlay');
  if (overlay) overlay.addEventListener('click', closeSidebar);

  // Close sidebar on escape key
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && sidebarOpen) closeSidebar();
  });

  // WS
  try { connectWS(); } catch (e) { console.warn('WS init failed:', e); }

  // Initial load
  loadConversations(true).catch(e => console.warn('Initial load failed:', e));

  // Fallback poll every 30s
  setInterval(() => loadConversations(false).catch(() => {}), 30000);
});
