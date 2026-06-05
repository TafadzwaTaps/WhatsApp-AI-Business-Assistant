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

  const q    = searchEl ? searchEl.value.trim().toLowerCase() : '';
  const safe = Array.isArray(convos) ? convos : [];

  // Update topbar unread badge on every render
  const totalUnread = safe.reduce((s, c) => s + (c.unread_count || 0), 0);
  const badge = document.getElementById('topbar-unread');
  if (badge) {
    if (totalUnread > 0) {
      badge.textContent = totalUnread > 99 ? '99+' : String(totalUnread);
      badge.style.display = 'inline-flex';
    } else {
      badge.style.display = 'none';
    }
    document.title = totalUnread > 0 ? `(${totalUnread}) WaziBot — Inbox` : 'WaziBot — Inbox';
  }

  let filtered = safe;
  // Handoff filter
  if (activeFilter === 'handoff') {
    filtered = safe.filter(c => c.in_handoff || c.handoff_state === 'human_handoff');
  } else if (activeFilter === 'unread') {
    filtered = safe.filter(c => (c.unread_count || 0) > 0);
  } else if (activeFilter === 'recent') {
    filtered = filtered.slice(0, 20);
  }
  // Full-text search: phone + customer_name + last_message
  const search = q;
  if (search) {
    filtered = filtered.filter(c =>
      (c.phone         || '').toLowerCase().includes(search) ||
      (c.customer_name || '').toLowerCase().includes(search) ||
      (c.last_message  || '').toLowerCase().includes(search)
    );
  }

  if (!filtered.length) {
    const emptyMsg = search
      ? `No results for "<strong>${escHtml(search)}</strong>"`
      : activeFilter === 'handoff' ? '🟢 No conversations in handoff mode'
      : activeFilter === 'unread'  ? '✅ All caught up — no unread messages'
      : 'No conversations yet';
    list.innerHTML = `<div class="empty-state">${emptyMsg}</div>`;
    return;
  }

  // Build via fragment — avoids full innerHTML reparse
  const frag = document.createDocumentFragment();
  filtered.forEach(c => {
    const div = document.createElement('div');
    div.className = 'contact-item' + (c.customer_id === currentCustomerId ? ' active' : '');
    div.dataset.customerId = c.customer_id;
    div.onclick = () => openChat(c.customer_id, c.phone || '', c.last_seen || '');
    const isHandoff = c.in_handoff || c.handoff_state === 'human_handoff';
    div.className = ['contact-item',
      c.customer_id === currentCustomerId ? 'active' : '',
      isHandoff ? 'handoff-active' : '',
    ].filter(Boolean).join(' ');

    // Highlight search match in phone display
    const rawPhone = c.phone || '—';
    const displayPhone = search
      ? rawPhone.replace(new RegExp('(' + search.replace(/[.*+?^${}()|[\]\\]/g,'\\$&') + ')', 'gi'),
          '<mark style="background:rgba(34,197,94,0.25);border-radius:2px;color:inherit;">$1</mark>')
      : escHtml(rawPhone);

    div.innerHTML = `
      <div class="contact-avatar">${isHandoff ? '🔴' : '👤'}</div>
      <div class="contact-info">
        <div class="contact-phone">${displayPhone}</div>
        <div class="contact-preview">${c.last_direction === 'outgoing' ? '🤖 ' : ''}${escHtml(safeText(c.last_message))}</div>
      </div>
      <div class="contact-meta">
        <div class="contact-time">${formatTime(c.last_message_at)}</div>
        ${(c.unread_count || 0) > 0 ? `<div class="unread-badge">${(c.unread_count||0) > 99 ? '99+' : c.unread_count}</div>` : ''}
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
  ac.style.display       = 'flex';
  ac.style.flexDirection = 'column';
  ac.style.overflow      = 'hidden';
  ac.style.flex          = '1';
  ac.style.minHeight     = '0'; /* desktop fix: flex child must shrink for inner scroll */

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
  // Load handoff state for this customer
  await loadHandoffState(customerId);
}

/* ── HANDOFF STATE ───────────────────────────────────────── */
let currentHandoffState = false;

async function loadHandoffState(customerId) {
  if (!customerId) return;
  try {
    const data = await apiFetch('/chat/handoff/pending');
    const pending = Array.isArray(data)
      ? data
      : (data && Array.isArray(data.data) ? data.data : []);
    const ids = pending.map(h => h.customer_id || h.id);
    currentHandoffState = ids.includes(customerId);
  } catch (_) {
    currentHandoffState = false;
  }
  updateHandoffUI(currentHandoffState);
}

function updateHandoffUI(isHandoff) {
  const btn     = document.getElementById('handoff-btn');
  const banner  = document.getElementById('handoff-banner');
  const bannerT = document.getElementById('handoff-banner-text');
  const bannerB = document.querySelector('.handoff-banner-btn');
  const input   = document.getElementById('send-input');
  const sbtn    = document.getElementById('send-btn');

  if (btn) {
    btn.textContent = isHandoff ? '👤 Agent' : '🤖 AI';
    btn.title       = isHandoff ? 'Switch back to AI mode' : 'Pause AI — take over';
    btn.style.color           = isHandoff ? 'var(--amber, #f59e0b)' : '';
    btn.style.borderColor     = isHandoff ? 'var(--amber, #f59e0b)' : '';
  }
  if (banner)  banner.style.display = isHandoff ? 'flex' : 'none';
  if (bannerT) bannerT.textContent  = isHandoff
    ? '🔴 Human agent mode — AI is paused. You are replying directly to the customer.'
    : '';
  if (bannerB) bannerB.textContent  = isHandoff ? '▶ Resume AI' : '⏸ Pause AI';
  if (input)   input.style.borderColor  = isHandoff ? 'rgba(245,158,11,0.6)' : '';
  if (sbtn)    sbtn.style.background    = isHandoff ? '#f59e0b' : '';
}

async function toggleHandoff() {
  if (!currentCustomerId) return;
  const btn = document.getElementById('handoff-btn');
  if (btn) btn.disabled = true;
  try {
    if (currentHandoffState) {
      await apiFetch(`/chat/handoff/${currentCustomerId}/release`, { method: 'POST' });
      currentHandoffState = false;
      showToast('✅ AI mode resumed — bot is handling replies again');
    } else {
      await apiFetch(`/chat/handoff/${currentCustomerId}/request`, { method: 'POST' });
      currentHandoffState = true;
      showToast('👤 Agent mode — AI paused. You are in control.');
    }
    updateHandoffUI(currentHandoffState);
    loadConversations(false).catch(() => {});
  } catch (e) {
    showToast('Handoff toggle failed: ' + e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ── DELETE CONVERSATION (CRUD) ──────────────────────────── */
function confirmDeleteConversation() {
  if (!currentCustomerId || !currentPhone) return;
  if (!confirm(`Delete all messages with ${currentPhone}?

This removes the conversation from your inbox. Cannot be undone.`)) return;
  deleteConversation(currentCustomerId);
}

async function deleteConversation(customerId) {
  try {
    await apiFetch(`/chat/conversations/${customerId}`, { method: 'DELETE' });
    showToast('🗑 Conversation deleted');
    allConversations = allConversations.filter(c => c.customer_id !== customerId);
    renderContacts(allConversations);
    currentCustomerId = null;
    currentPhone      = null;
    const noSel = document.getElementById('no-selection');
    const ac    = document.getElementById('active-chat');
    if (noSel) noSel.style.display = 'flex';
    if (ac)    ac.style.display    = 'none';
  } catch (e) {
    showToast('Delete failed: ' + e.message, true);
  }
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

  // Determine sender type for visual differentiation
  // sender_type: "ai" = bot reply, "agent" = human agent reply, undefined = customer
  const senderType = msg.sender_type || (dir === 'outgoing' ? 'ai' : 'customer');
  const isAgentMsg  = senderType === 'agent';
  const isAiMsg     = senderType === 'ai';

  const div = document.createElement('div');
  div.className = [
    'msg',
    dir,
    isBroadcast ? 'broadcast' : '',
    isAgentMsg  ? 'msg-agent' : '',
    isAiMsg     ? 'msg-ai'    : '',
  ].filter(Boolean).join(' ');
  div.dataset.msgId     = msg.id || '';
  div.dataset.senderType = senderType;

  const displayText = isBroadcast
    ? '📢 ' + escHtml(text.replace('[BROADCAST] ', ''))
    : escHtml(text);

  // Sender label: shown above agent messages so staff can identify themselves
  const senderLabel = isAgentMsg
    ? `<div class="msg-sender-label">👤 ${escHtml(msg.sender_name || 'Agent')}</div>`
    : isAiMsg && dir === 'outgoing'
    ? `<div class="msg-sender-label msg-ai-label">🤖 AI</div>`
    : '';

  div.innerHTML = `
    ${senderLabel}
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

  // Optimistic render — mark as agent message so it shows agent badge immediately
  appendBubble({
    id: null,
    text,
    direction:   'outgoing',
    sender_type: 'agent',     // human agent is typing this
    sender_name: 'You',
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


/* ══════════════════════════════════════════════════════════
   CLEAR INBOX — clear read chats or all chats
══════════════════════════════════════════════════════════ */

async function clearReadChats() {
  // Find all conversations with unread_count === 0 and delete them
  const readConvos = allConversations.filter(c => (c.unread_count || 0) === 0);
  if (!readConvos.length) {
    showToast('No read conversations to clear.');
    return;
  }
  if (!confirm(`Clear ${readConvos.length} read conversation${readConvos.length !== 1 ? 's' : ''}? Messages will be removed from the inbox.`)) return;

  let cleared = 0;
  for (const c of readConvos) {
    try {
      await apiFetch(`/chat/conversations/${c.customer_id}`, { method: 'DELETE' });
      cleared++;
    } catch (_) {}
  }

  allConversations = allConversations.filter(c => (c.unread_count || 0) > 0);
  renderContacts(allConversations);
  showToast(`🗑 Cleared ${cleared} read conversation${cleared !== 1 ? 's' : ''}`);

  // If the open chat was one of the cleared ones, close it
  const openStillExists = allConversations.some(c => c.customer_id === currentCustomerId);
  if (!openStillExists) {
    currentCustomerId = null;
    currentPhone      = null;
    const noSel = document.getElementById('no-selection');
    const ac    = document.getElementById('active-chat');
    if (noSel) noSel.style.display = 'flex';
    if (ac)    ac.style.display    = 'none';
  }
}

async function confirmClearAll() {
  const total = allConversations.length;
  if (!total) {
    showToast('Inbox is already empty.');
    return;
  }
  if (!confirm(`Clear ALL ${total} conversations?

This removes all message history from the inbox. Customer and order records are kept.

This cannot be undone.`)) return;

  let cleared = 0;
  for (const c of allConversations) {
    try {
      await apiFetch(`/chat/conversations/${c.customer_id}`, { method: 'DELETE' });
      cleared++;
    } catch (_) {}
  }

  allConversations = [];
  renderContacts([]);
  currentCustomerId = null;
  currentPhone      = null;

  const noSel = document.getElementById('no-selection');
  const ac    = document.getElementById('active-chat');
  if (noSel) noSel.style.display = 'flex';
  if (ac)    ac.style.display    = 'none';

  showToast(`🗑 Cleared ${cleared} conversation${cleared !== 1 ? 's' : ''}`);
}


/* ══════════════════════════════════════════════════════════
   QUICK ACTIONS BAR — Phase 2
   Show / hide when a conversation is opened.
   All functions use existing apiFetch() and showToast().
══════════════════════════════════════════════════════════ */

function showQuickActions() {
  const bar = document.getElementById('quick-actions-bar');
  if (bar) bar.style.display = 'flex';
}

function hideQuickActions() {
  const bar = document.getElementById('quick-actions-bar');
  if (bar) bar.style.display = 'none';
}

// Hook into existing openChat — show bar when a chat opens
const _origOpenChat = typeof openChat === 'function' ? openChat : null;
// We patch via a wrapper so existing openChat logic is unchanged
(function() {
  const _orig = window.openChat;
  window.openChat = async function(customerId, phone, lastSeen) {
    await _orig.call(this, customerId, phone, lastSeen);
    showQuickActions();
  };
})();

// ── Quick action handlers ───────────────────────────────────────────

async function qaRepeatLastOrder() {
  if (!currentCustomerId) return;
  const msg = "🔄 *Repeating your last order*\n\nJust reply *yes* to confirm and I'll add it to your cart!";
  await _qaSend(msg, 'Repeat order message sent ✓');
}

async function qaRequestPayment() {
  if (!currentCustomerId || !currentPhone) return;

  // Try to find an unpaid order for this customer
  let paymentMsg = "💳 *Payment Reminder*\n\nYou have a pending payment. Please complete your payment to confirm your order.\n\nType *help* if you need the payment details again.";

  try {
    // Fetch pending reminders for the business (uses existing endpoint)
    const reminders = await apiFetch('/payments/reminders/pending');
    const orders    = reminders.orders || [];
    const match     = orders.find(o => o.customer_phone === currentPhone);
    if (match) {
      const ref   = `ORDER-${match.order_id}`;
      const total = parseFloat(match.total_price || 0).toFixed(2);
      const method = (match.payment_method || 'EcoCash').replace(/_/g,' ').replace(/\b\w/g, c => c.toUpperCase());
      paymentMsg = `💳 *Payment Due*\n\n📦 Order: *${ref}*\n💰 Amount: *$${total}*\n📱 Method: *${method}*\n\nPlease complete your payment to confirm your order. Reply *paid* once done.`;
    }
  } catch (_) {}

  await _qaSend(paymentMsg, 'Payment request sent ✓');
}

async function qaMarkPaid() {
  if (!currentCustomerId || !currentPhone) return;

  // Find the most recent stale order for this customer
  let orderFound = false;
  try {
    const reminders = await apiFetch('/payments/reminders/pending');
    const orders    = (reminders.orders || []).filter(o => o.customer_phone === currentPhone);
    if (orders.length > 0) {
      const order = orders[0];
      if (!confirm(`Mark ORDER-${order.order_id} ($${parseFloat(order.total_price||0).toFixed(2)}) as PAID?`)) return;
      await apiFetch(`/payments/reminders/${order.order_id}/nudge?dry_run=false`, { method: 'POST' });
      // Manually confirm via payment endpoint
      await apiFetch(`/payments/manual/confirm`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ order_id: order.order_id, reference: `ORDER-${order.order_id}`, amount: parseFloat(order.total_price || 0) }),
      });
      showToast(`✅ ORDER-${order.order_id} marked as paid`);
      orderFound = true;
    }
  } catch (e) {
    showToast('⚠ Could not mark paid: ' + e.message, true);
    return;
  }
  if (!orderFound) showToast('ℹ No pending orders found for this customer');
}

async function qaCreateDelivery() {
  if (!currentCustomerId) return;
  const msg = "🚚 *Delivery Confirmation*\n\nPlease send your *full delivery address* (street, suburb, city) and we'll arrange delivery for your order.";
  await _qaSend(msg, 'Delivery request sent ✓');
}

async function qaViewOrders() {
  if (!currentPhone) return;
  // Open the dashboard orders page filtered to this phone in a new tab
  const dashUrl = `/dashboard#orders?phone=${encodeURIComponent(currentPhone)}`;
  window.open(dashUrl, '_blank');
}

async function qaGenerateInvoice() {
  if (!currentCustomerId || !currentPhone) return;

  // Find the most recent order for this customer to get an order_id
  let invoiceSent = false;
  try {
    const reminders = await apiFetch('/payments/reminders/pending');
    const orders    = (reminders.orders || []).filter(o => o.customer_phone === currentPhone);
    if (orders.length > 0) {
      const orderId = orders[0].order_id;
      const invoiceUrl = `${window.location.origin}/invoice/${orderId}`;
      const msg = `🧾 *Invoice for ORDER-${orderId}*\n\nYou can download your invoice here:\n${invoiceUrl}`;
      await _qaSend(msg, `Invoice link sent for ORDER-${orderId} ✓`);
      invoiceSent = true;
    }
  } catch (_) {}

  if (!invoiceSent) {
    showToast('ℹ No recent orders found for this customer');
  }
}

// ── Internal sender ───────────────────────────────────────

async function _qaSend(text, successMsg) {
  if (!currentCustomerId || !text) return;
  try {
    const allBtns = document.querySelectorAll('.qa-btn');
    allBtns.forEach(b => { b.disabled = true; });

    await apiFetch('/chat/send', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ customer_id: currentCustomerId, text }),
    });

    // Reload messages to show what was sent
    await loadMessages(currentCustomerId, false);
    showToast(successMsg || 'Sent ✓');
  } catch (e) {
    showToast('⚠ Send failed: ' + e.message, true);
  } finally {
    const allBtns = document.querySelectorAll('.qa-btn');
    allBtns.forEach(b => { b.disabled = false; });
  }
}


/* ══════════════════════════════════════════════════════════
   UX ENHANCEMENTS — Phases 1-7
   All additive. Existing functions unchanged.
══════════════════════════════════════════════════════════ */

/* ── Phase 1: Handoff reason & conversation summary ── */

const HANDOFF_REASONS = [
  'Payment Issue', 'Refund Request', 'Delivery Problem',
  'Complaint', 'Complex Order', 'Product Question', 'Technical Issue', 'Other'
];
let _selectedReason = 'Other';
let _handoffPriority = 'normal';

function openHandoffReasonModal() {
  const modal = document.getElementById('handoff-reason-modal');
  const grid  = document.getElementById('reason-chips');
  if (!modal || !grid) return;

  grid.innerHTML = HANDOFF_REASONS.map(r =>
    `<div class="reason-chip ${r === _selectedReason ? 'selected' : ''}"
          onclick="selectReason('${r}')">${r}</div>`
  ).join('');

  document.getElementById('reason-custom').value = '';
  modal.classList.add('open');
}

function closeHandoffReasonModal() {
  const modal = document.getElementById('handoff-reason-modal');
  if (modal) modal.classList.remove('open');
}

function selectReason(r) {
  _selectedReason = r;
  document.querySelectorAll('.reason-chip').forEach(c => {
    c.classList.toggle('selected', c.textContent === r);
  });
}

async function confirmHandoffWithReason() {
  if (!currentCustomerId) return;
  const custom = (document.getElementById('reason-custom')?.value || '').trim();
  const reason = custom || _selectedReason;
  closeHandoffReasonModal();

  try {
    await apiFetch(`/chat/handoff/${currentCustomerId}/request-with-reason`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ reason, priority: _handoffPriority }),
    });
    currentHandoffState = true;
    updateHandoffUI(true);
    showToast(`👤 Agent mode — ${reason}`);
    await loadConvSummary(currentCustomerId);
  } catch (e) {
    showToast('⚠ Handoff failed: ' + e.message, true);
  }
}

// Patch existing toggleHandoff to open reason modal when pausing
(function() {
  const _orig = window.toggleHandoff;
  window.toggleHandoff = async function() {
    if (!currentHandoffState) {
      // Opening handoff — show reason modal
      openHandoffReasonModal();
    } else {
      // Releasing handoff — use existing logic
      await _orig.call(this);
    }
  };
})();

// Conversation summary
async function loadConvSummary(customerId) {
  const panel = document.getElementById('conv-summary-panel');
  if (!panel) return;
  if (!customerId) { panel.classList.remove('visible'); return; }

  try {
    const data = await apiFetch(`/chat/handoff/${customerId}/summary`);
    const chips = [
      data.segment === 'vip'   ? `<span class="conv-summary-chip vip">⭐ VIP</span>` :
      data.segment === 'loyal' ? `<span class="conv-summary-chip loyal">💚 Loyal</span>` : '',
      data.order_count > 0 ? `<span class="conv-summary-chip">🛒 ${data.order_count} orders</span>` : '',
      data.total_spent > 0 ? `<span class="conv-summary-chip">💰 $${parseFloat(data.total_spent).toFixed(2)}</span>` : '',
      data.handoff_reason ? `<span class="conv-summary-chip urgent">📌 ${escHtml(data.handoff_reason)}</span>` : '',
      data.pending_payment ? `<span class="conv-summary-chip urgent">💳 ${escHtml(data.pending_payment)}</span>` : '',
    ].filter(Boolean).join('');

    panel.innerHTML = chips || `<span class="conv-summary-chip">New Customer</span>`;
    panel.classList.toggle('visible', currentHandoffState);
  } catch (_) {
    panel.classList.remove('visible');
  }
}

// Patch openChat to also load summary
(function() {
  const _orig = window.openChat;
  window.openChat = async function(customerId, phone, lastSeen) {
    await _orig.call(this, customerId, phone, lastSeen);
    await loadConvSummary(customerId);
    await loadAgentNotes(customerId);
    showQuickActions(); // from Phase 2 quick-actions
  };
})();

// Agent notes
let _agentNotesOpen = false;

function toggleAgentNotes() {
  _agentNotesOpen = !_agentNotesOpen;
  const panel = document.getElementById('agent-notes-panel');
  if (panel) panel.classList.toggle('open', _agentNotesOpen);
}

async function loadAgentNotes(customerId) {
  if (!customerId) return;
  try {
    const data  = await apiFetch(`/chat/handoff/${customerId}/notes`);
    const notes = data.notes || [];
    const list  = document.getElementById('agent-note-list');
    const cnt   = document.getElementById('agent-notes-count');
    if (cnt) cnt.textContent = notes.length ? ` (${notes.length})` : '';
    if (list) {
      list.innerHTML = notes.length
        ? notes.map(n => `
            <div class="agent-note-item">
              ${escHtml(n.text)}
              <div class="agent-note-meta">— ${escHtml(n.agent || 'agent')} · ${(n.timestamp||'').slice(0,16).replace('T',' ')}</div>
            </div>`).join('')
        : '<div style="color:var(--text-muted);font-size:11px;padding:6px 0;">No notes yet.</div>';
    }
  } catch (_) {}
}

async function saveAgentNote() {
  const input = document.getElementById('agent-note-input');
  const text  = (input?.value || '').trim();
  if (!text || !currentCustomerId) return;
  try {
    await apiFetch(`/chat/handoff/${currentCustomerId}/note?note_text=${encodeURIComponent(text)}`, { method: 'POST' });
    input.value = '';
    await loadAgentNotes(currentCustomerId);
    showToast('Note saved ✓');
  } catch (e) {
    showToast('Failed to save note', true);
  }
}


/* ── Phase 2: Help Panel / Support Assistant ── */

let _helpOpen = false;
const _helpCurrentPage = 'inbox';

function toggleHelp() {
  _helpOpen = !_helpOpen;
  document.getElementById('help-panel')?.classList.toggle('open', _helpOpen);
  if (_helpOpen) setTimeout(() => document.getElementById('help-input')?.focus(), 50);
}
function closeHelp() {
  _helpOpen = false;
  document.getElementById('help-panel')?.classList.remove('open');
}
function askHelpQ(q) {
  const inp = document.getElementById('help-input');
  if (inp) inp.value = q;
  askHelp();
}
async function askHelp() {
  const q = (document.getElementById('help-input')?.value || '').trim();
  if (!q) return;
  const body = document.getElementById('help-body');
  if (!body) return;
  body.innerHTML = '<div style="color:var(--text-dim);font-size:12px;">Looking up…</div>';
  try {
    const data = await apiFetch('/support/ask', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ question: q, context: _helpCurrentPage }),
    });
    let html = `<div class="help-answer">${escHtml(data.answer).replace(/\*(.*?)\*/g, '<strong>$1</strong>')}</div>`;
    if (data.steps?.length) {
      html += `<ol class="help-steps">${data.steps.map(s => `<li>${escHtml(s)}</li>`).join('')}</ol>`;
    }
    if (data.tips?.length) {
      html += data.tips.map(t => `<div class="help-tip">💡 ${escHtml(t)}</div>`).join('');
    }
    if (data.related?.length) {
      html += `<div class="help-related">${data.related.map(r =>
        `<button class="help-related-chip" onclick="askHelpQ('Tell me about ${escHtml(r.title)}')">${escHtml(r.title)}</button>`
      ).join('')}</div>`;
    }
    body.innerHTML = html;
  } catch (e) {
    body.innerHTML = `<div style="color:var(--red);font-size:12px;">Could not load answer: ${e.message}</div>`;
  }
}

// Close help on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && _helpOpen) { closeHelp(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); openCmd(); }
});


/* ── Phase 5: Command Palette ── */

const CMD_COMMANDS = [
  { icon: '💬', label: 'Open Inbox',         sub: 'View all conversations',    action: () => {} },
  { icon: '📞', label: 'View Customers',      sub: 'Browse CRM customer list',  action: () => window.open('/dashboard#customers','_self') },
  { icon: '📣', label: 'Create Campaign',     sub: 'Send a targeted message',   action: () => window.open('/dashboard#campaigns','_self') },
  { icon: '📦', label: 'View Orders',         sub: 'See pending and recent orders', action: () => window.open('/dashboard#orders','_self') },
  { icon: '📊', label: 'Open Analytics',      sub: 'Revenue and stats',         action: () => window.open('/dashboard#analytics','_self') },
  { icon: '📋', label: 'View Inventory',      sub: 'Products and stock',        action: () => window.open('/dashboard#inventory','_self') },
  { icon: '⭐', label: 'Show VIP Customers',  sub: 'High-value customer list',  action: () => window.open('/dashboard#customers?segment=vip','_self') },
  { icon: '🔴', label: 'Handoff Queue',       sub: 'Conversations needing agent', action: () => setFilter('handoff') },
  { icon: '💳', label: 'Pending Payments',    sub: 'Orders awaiting payment',   action: () => window.open('/dashboard#payments','_self') },
  { icon: '💡', label: 'Growth Opportunities', sub: 'Retention and revenue insights', action: () => window.open('/dashboard#overview','_self') },
  { icon: '❓', label: 'Ask for Help',        sub: 'Open the support assistant', action: () => { closeCmd(); toggleHelp(); } },
  { icon: '🔍', label: 'Search Conversations', sub: 'Find a customer',          action: () => document.getElementById('search-input')?.focus() },
];

let _cmdOpen   = false;
let _cmdActive = 0;
let _cmdFiltered = CMD_COMMANDS;

function openCmd() {
  _cmdOpen = true;
  _cmdActive = 0;
  _cmdFiltered = CMD_COMMANDS;
  document.getElementById('cmd-overlay')?.classList.add('open');
  const inp = document.getElementById('cmd-input');
  if (inp) { inp.value = ''; inp.focus(); }
  renderCmdResults();
}
function closeCmd() {
  _cmdOpen = false;
  document.getElementById('cmd-overlay')?.classList.remove('open');
}

function renderCmdResults() {
  const q    = (document.getElementById('cmd-input')?.value || '').toLowerCase();
  _cmdFiltered = q
    ? CMD_COMMANDS.filter(c => c.label.toLowerCase().includes(q) || c.sub.toLowerCase().includes(q))
    : CMD_COMMANDS;

  const box = document.getElementById('cmd-results');
  if (!box) return;

  if (!_cmdFiltered.length) {
    box.innerHTML = `<div style="padding:16px;text-align:center;color:var(--text-dim);font-size:12px;">No results for "${escHtml(q)}"</div>`;
    return;
  }

  box.innerHTML = _cmdFiltered.map((c, i) => `
    <div class="cmd-result ${i === _cmdActive ? 'active' : ''}" onclick="execCmd(${i})">
      <span class="cmd-result-icon">${c.icon}</span>
      <div>
        <div class="cmd-result-text">${escHtml(c.label)}</div>
        <div class="cmd-result-sub">${escHtml(c.sub)}</div>
      </div>
    </div>
  `).join('');
}

function execCmd(idx) {
  const cmd = _cmdFiltered[idx];
  if (!cmd) return;
  closeCmd();
  cmd.action();
}

function cmdKeyDown(e) {
  if (e.key === 'Escape')    { closeCmd(); return; }
  if (e.key === 'ArrowDown') { _cmdActive = Math.min(_cmdActive + 1, _cmdFiltered.length - 1); renderCmdResults(); e.preventDefault(); return; }
  if (e.key === 'ArrowUp')   { _cmdActive = Math.max(_cmdActive - 1, 0); renderCmdResults(); e.preventDefault(); return; }
  if (e.key === 'Enter')     { execCmd(_cmdActive); e.preventDefault(); }
}
