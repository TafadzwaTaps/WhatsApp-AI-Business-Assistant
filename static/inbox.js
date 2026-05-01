/* ═══════════════════════════════════════════════════════════
   WaziBot Inbox — inbox.js  v3.0
   ─ Real-time via WebSocket + polling fallback
   ─ Delete / clear messages
   ─ Date separators, full scroll, message search
   ─ Star, copy, reply-quote, emoji reactions
   ─ Contact info panel
   ─ Timestamp fix (always appends Z when missing)
   ═══════════════════════════════════════════════════════════ */

'use strict';

/* ── CONFIG & SESSION ───────────────────────────────────── */
const API   = 'https://wazibot-api-assistant.onrender.com';
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
const MSG_LIMIT       = 60;
let hasMoreMessages   = false;
let wsConn            = null;
let wsRetryDelay      = 3000;
let activeFilter      = 'all';
let sidebarOpen       = false;
let infoPanelOpen     = false;
let msgSearchOpen     = false;
let lastKnownMsgId    = 0;           // tracks newest received message for live poll
let pollTimer         = null;

/* ── THEME ──────────────────────────────────────────────── */
function applyTheme(theme) {
  document.body.classList.toggle('light', theme === 'light');
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = theme === 'light' ? '🌙' : '☀️';
  localStorage.setItem('wazi_inbox_theme', theme);
}
function toggleTheme() {
  applyTheme(document.body.classList.contains('light') ? 'dark' : 'light');
}
function initTheme() {
  const saved = localStorage.getItem('wazi_inbox_theme') ||
                (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  applyTheme(saved);
}

/* ── MOBILE SIDEBAR ─────────────────────────────────────── */
function openSidebar() {
  sidebarOpen = true;
  document.getElementById('sidebar')?.classList.add('open');
  document.getElementById('sidebar-overlay')?.classList.add('open');
}
function closeSidebar() {
  sidebarOpen = false;
  document.getElementById('sidebar')?.classList.remove('open');
  document.getElementById('sidebar-overlay')?.classList.remove('open');
}
function toggleSidebar() { sidebarOpen ? closeSidebar() : openSidebar(); }

/* ── API HELPER ─────────────────────────────────────────── */
async function apiFetch(path, opts = {}) {
  try {
    const res = await fetch(API + path, {
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      ...opts,
    });
    if (res.status === 401) { window.location.href = '/dashboard'; throw new Error('Unauthorized'); }
    if (!res.ok) {
      let msg = res.statusText || 'API error';
      try { const e = await res.json(); msg = e.detail || msg; } catch {}
      throw new Error(msg);
    }
    return res.json();
  } catch (err) {
    if (err.message === 'Unauthorized') throw err;
    if (err instanceof TypeError) throw new Error('Cannot reach server');
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
    wsRetryDelay = 3000;
    // Heartbeat every 25s
    const hb = setInterval(() => {
      if (wsConn?.readyState === WebSocket.OPEN) wsConn.send(JSON.stringify({ type: 'ping' }));
      else clearInterval(hb);
    }, 25000);
  };

  wsConn.onclose = () => {
    setWsStatus('dead', 'Reconnecting…');
    setTimeout(connectWS, wsRetryDelay);
    wsRetryDelay = Math.min(wsRetryDelay * 1.5, 30000);
  };

  wsConn.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload.type === 'pong') return;

      if (payload.event === 'new_message') {
        const { customer_id, message } = payload;

        if (customer_id === currentCustomerId && message) {
          // Check we don't already have this message rendered
          if (message.id && document.querySelector(`[data-msg-id="${message.id}"]`)) return;
          appendBubble(message);
          scrollToBottom();
          if (message.direction === 'incoming') markRead().catch(() => {});
          if (message.id) lastKnownMsgId = Math.max(lastKnownMsgId, message.id);
        }

        loadConversations(false).catch(() => {});
      }
    } catch (err) { console.warn('WS parse error:', err); }
  };

  wsConn.onerror = () => wsConn.close();
}

function setWsStatus(cls, label) {
  const dot = document.getElementById('ws-dot');
  const lbl = document.getElementById('ws-label');
  if (dot) dot.className = 'ws-dot ' + cls;
  if (lbl) lbl.textContent = label;
}

/* ── POLLING FALLBACK (catches messages missed by WS) ───── */
function startPoll() {
  stopPoll();
  // Poll every 8s when a chat is open — catches any messages WS may have missed
  pollTimer = setInterval(async () => {
    if (!currentCustomerId) return;
    try {
      const data = await apiFetch(`/chat/messages/${currentCustomerId}?limit=10&offset=0`);
      const msgs = data?.messages || [];
      let added = 0;
      msgs.forEach(m => {
        if (m.id && m.id > lastKnownMsgId) {
          if (!document.querySelector(`[data-msg-id="${m.id}"]`)) {
            appendBubble(m);
            added++;
          }
          lastKnownMsgId = Math.max(lastKnownMsgId, m.id);
        }
      });
      if (added > 0) {
        scrollToBottom();
        loadConversations(false).catch(() => {});
      }
    } catch {}
  }, 8000);
}
function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

/* ── CONVERSATIONS SIDEBAR ──────────────────────────────── */
async function loadConversations(showSkeleton = true) {
  try {
    if (showSkeleton) showSkeletons();
    const unread = activeFilter === 'unread';
    const raw = await apiFetch(`/chat/conversations?unread_only=${unread}`);
    allConversations = Array.isArray(raw) ? raw : (raw?.data || []);
    renderContacts(allConversations);
  } catch (e) {
    const list = document.getElementById('contact-list');
    if (list) list.innerHTML = `<div class="empty-state" style="color:var(--red)">⚠ ${escHtml(e.message)}</div>`;
  }
}

function showSkeletons() {
  const list = document.getElementById('contact-list');
  if (!list) return;
  list.innerHTML = `<div class="skeleton-list">
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

  const search = searchEl?.value.trim().toLowerCase() || '';
  let filtered = Array.isArray(convos) ? convos : [];
  if (search) filtered = filtered.filter(c => (c.phone || '').includes(search));
  if (activeFilter === 'recent') filtered = filtered.slice(0, 20);

  if (!filtered.length) {
    list.innerHTML = `<div class="empty-state">${search ? `No results for "${escHtml(search)}"` : 'No conversations yet'}</div>`;
    return;
  }

  const frag = document.createDocumentFragment();
  filtered.forEach(c => {
    const div = document.createElement('div');
    div.className = 'contact-item' + (c.customer_id === currentCustomerId ? ' active' : '');
    div.dataset.customerId = c.customer_id;
    div.onclick = () => openChat(c.customer_id, c.phone || '', c.last_seen || '');

    // Right-click / long-press context menu on contact
    div.oncontextmenu = (e) => { e.preventDefault(); showContactMenu(e, c); };

    div.innerHTML = `
      <div class="contact-avatar">${avatarInitials(c.phone)}</div>
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

function avatarInitials(phone) {
  if (!phone) return '👤';
  const n = safeText(phone).replace(/\D/g, '');
  return n.slice(-2) || '??';
}

function filterContacts(val) { renderContacts(allConversations); }

function setFilter(f) {
  activeFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('filter-' + f)?.classList.add('active');
  loadConversations(false).catch(() => {});
}

/* ── CONTACT CONTEXT MENU ───────────────────────────────── */
function showContactMenu(e, contact) {
  closeAllMenus();
  const menu = document.createElement('div');
  menu.className = 'ctx-menu';
  menu.id = 'ctx-menu';
  menu.style.top  = e.clientY + 'px';
  menu.style.left = e.clientX + 'px';
  menu.innerHTML = `
    <div class="ctx-item" onclick="openChat(${contact.customer_id},'${escHtml(contact.phone||'')}','');closeAllMenus()">💬 Open chat</div>
    <div class="ctx-item" onclick="copyToClipboard('${escHtml(contact.phone||'')}','Phone copied');closeAllMenus()">📋 Copy number</div>
    <div class="ctx-sep"></div>
    <div class="ctx-item ctx-danger" onclick="confirmClearMessages(${contact.customer_id},'${escHtml(contact.phone||'')}');closeAllMenus()">🗑 Clear messages</div>`;
  document.body.appendChild(menu);
  setTimeout(() => document.addEventListener('click', closeAllMenus, { once: true }), 50);
}

/* ── OPEN CHAT ──────────────────────────────────────────── */
async function openChat(customerId, phone, lastSeen) {
  if (!customerId || !phone) return;

  currentCustomerId = customerId;
  currentPhone      = phone;
  msgOffset         = 0;
  lastKnownMsgId    = 0;
  infoPanelOpen     = false;

  if (window.innerWidth < 900) closeSidebar();

  document.getElementById('no-selection')?.style.setProperty('display', 'none');

  const ac = document.getElementById('active-chat');
  if (ac) { ac.style.display = 'flex'; ac.style.flexDirection = 'column'; }

  document.getElementById('chat-phone')?.textContent !== undefined &&
    (document.getElementById('chat-phone').textContent = phone);

  const statusEl = document.getElementById('chat-status');
  if (statusEl) statusEl.textContent = lastSeen ? `Last seen ${formatTime(lastSeen)}` : 'Customer';

  // Update active highlight
  document.querySelectorAll('.contact-item').forEach(el =>
    el.classList.toggle('active', parseInt(el.dataset.customerId) === customerId)
  );

  // Show info panel phone
  const infoPh = document.getElementById('info-phone');
  if (infoPh) infoPh.textContent = phone;
  const infoSeen = document.getElementById('info-last-seen');
  if (infoSeen) infoSeen.textContent = lastSeen ? formatTime(lastSeen) : '—';

  // Close msg search if open
  closeMsgSearch();

  // Clear + load
  const mc = document.getElementById('chat-messages');
  if (mc) mc.innerHTML = `<div class="msgs-loading">Loading messages…</div>`;
  document.getElementById('load-more-btn')?.style.setProperty('display', 'none');

  await loadMessages(customerId, true);
  await markRead();

  // Start polling for new messages
  startPoll();
}

/* ── LOAD MESSAGES ──────────────────────────────────────── */
async function loadMessages(customerId, reset = false) {
  if (reset) msgOffset = 0;
  try {
    const data  = await apiFetch(`/chat/messages/${customerId}?limit=${MSG_LIMIT}&offset=${msgOffset}`);
    const msgs  = data?.messages || [];
    const container = document.getElementById('chat-messages');
    if (!container) return;
    const lmBtn = document.getElementById('load-more-btn');

    if (reset) {
      container.innerHTML = '';
      if (lmBtn) { lmBtn.style.display = 'none'; container.appendChild(lmBtn); }
    }

    if (msgs.length === 0 && reset) {
      const empty = document.createElement('div');
      empty.className = 'msgs-empty';
      empty.textContent = 'No messages yet — say hello! 👋';
      container.appendChild(empty);
      return;
    }

    // Track newest id
    msgs.forEach(m => { if (m.id) lastKnownMsgId = Math.max(lastKnownMsgId, m.id); });

    // Build with date separators
    const frag = document.createDocumentFragment();
    let lastDay = null;

    msgs.forEach(m => {
      const day = dayLabel(m.created_at);
      if (day !== lastDay) {
        frag.appendChild(makeDateSep(day));
        lastDay = day;
      }
      frag.appendChild(createBubble(m));
    });

    if (reset) {
      container.appendChild(frag);
    } else {
      // Prepend older messages after the load-more button
      const anchor = lmBtn?.nextSibling || container.children[1] || null;
      container.insertBefore(frag, anchor);
    }

    hasMoreMessages = msgs.length === MSG_LIMIT;
    if (lmBtn) lmBtn.style.display = hasMoreMessages ? 'block' : 'none';

    if (reset) scrollToBottom();

  } catch (e) {
    const container = document.getElementById('chat-messages');
    if (container) container.innerHTML =
      `<div class="msgs-error">⚠ ${escHtml(e.message)}</div>`;
  }
}

async function loadMoreMessages() {
  if (!currentCustomerId) return;
  const container = document.getElementById('chat-messages');
  const prevHeight = container?.scrollHeight || 0;
  msgOffset += MSG_LIMIT;
  await loadMessages(currentCustomerId, false);
  // Keep scroll position stable after prepend
  if (container) container.scrollTop += (container.scrollHeight - prevHeight);
}

/* ── DATE SEPARATOR ─────────────────────────────────────── */
function makeDateSep(label) {
  const div = document.createElement('div');
  div.className = 'date-sep';
  div.innerHTML = `<span>${escHtml(label)}</span>`;
  return div;
}

function dayLabel(iso) {
  if (!iso) return 'Unknown date';
  const d = parseDate(iso);
  if (!d || isNaN(d)) return 'Unknown date';
  const now   = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yest  = new Date(today - 86400000);
  const msgDay= new Date(d.getFullYear(), d.getMonth(), d.getDate());
  if (+msgDay === +today) return 'Today';
  if (+msgDay === +yest)  return 'Yesterday';
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' });
}

/* ── BUBBLE FACTORY ─────────────────────────────────────── */
function createBubble(msg) {
  const text = safeText(msg.text);
  const isBroadcast = text.startsWith('[BROADCAST]');
  const dir  = (msg.direction || '').startsWith('in') ? 'incoming' : 'outgoing';
  const isStarred = (msg.starred || localStorage.getItem(`star_${msg.id}`) === '1');

  const div = document.createElement('div');
  div.className = `msg ${dir}${isBroadcast ? ' broadcast' : ''}${isStarred ? ' starred' : ''}`;
  if (msg.id) div.dataset.msgId = msg.id;

  const displayText = isBroadcast
    ? '📢 ' + escHtml(text.replace('[BROADCAST] ', ''))
    : formatMessageText(text);

  div.innerHTML = `
    <div class="bubble" onclick="showMsgMenu(event, this, '${esc(msg.id)}', '${esc(text)}')">
      ${displayText}
      ${isStarred ? '<span class="star-badge" title="Starred">⭐</span>' : ''}
    </div>
    <div class="msg-meta">
      <span class="msg-time" title="${escHtml(fullTimestamp(msg.created_at))}">${formatTime(msg.created_at)}</span>
      ${dir === 'outgoing' ? `<span class="msg-status ${msg.status || 'sent'}"></span>` : ''}
    </div>`;
  return div;
}

function appendBubble(msg) {
  const container = document.getElementById('chat-messages');
  if (!container) return;

  // Insert date sep if this is a different day from the last bubble
  const lastBubble = container.lastElementChild;
  const lastTs = lastBubble?.dataset?.ts || null;
  const newDay  = dayLabel(msg.created_at);
  const lastDay = lastTs ? dayLabel(lastTs) : null;
  if (newDay !== lastDay) container.appendChild(makeDateSep(newDay));

  const bubble = createBubble(msg);
  if (msg.created_at) bubble.dataset.ts = msg.created_at;
  container.appendChild(bubble);
}

/* Format message text — turns *bold*, line breaks, URLs into HTML */
function formatMessageText(text) {
  let s = escHtml(text);
  // Bold: *text*
  s = s.replace(/\*([^*\n]+)\*/g, '<strong>$1</strong>');
  // Italic: _text_
  s = s.replace(/_([^_\n]+)_/g, '<em>$1</em>');
  // URLs
  s = s.replace(/(https?:\/\/[^\s<>"]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  return s;
}

/* ── MESSAGE CONTEXT MENU ───────────────────────────────── */
function showMsgMenu(event, bubbleEl, msgId, text) {
  event.stopPropagation();
  closeAllMenus();

  const rect = bubbleEl.getBoundingClientRect();
  const menu = document.createElement('div');
  menu.className = 'ctx-menu msg-ctx-menu';
  menu.id = 'ctx-menu';

  const isStar = localStorage.getItem(`star_${msgId}`) === '1';

  menu.innerHTML = `
    <div class="ctx-item" onclick="copyToClipboard('${esc(text)}','Copied');closeAllMenus()">📋 Copy</div>
    <div class="ctx-item" onclick="replyQuote('${esc(text)}');closeAllMenus()">↩️ Reply</div>
    <div class="ctx-item" onclick="toggleStar('${esc(msgId)}');closeAllMenus()">${isStar ? '✩ Unstar' : '⭐ Star'}</div>
    <div class="ctx-sep"></div>
    <div class="ctx-item ctx-danger" onclick="deleteMessage('${esc(msgId)}');closeAllMenus()">🗑 Delete</div>`;

  // Position near bubble
  menu.style.top  = (rect.bottom + window.scrollY + 4) + 'px';
  menu.style.left = Math.min(rect.left + window.scrollX, window.innerWidth - 200) + 'px';
  document.body.appendChild(menu);
  setTimeout(() => document.addEventListener('click', closeAllMenus, { once: true }), 50);
}

function closeAllMenus() {
  document.getElementById('ctx-menu')?.remove();
}

/* ── STAR / DELETE / CLEAR ──────────────────────────────── */
function toggleStar(msgId) {
  if (!msgId) return;
  const isStar = localStorage.getItem(`star_${msgId}`) === '1';
  localStorage.setItem(`star_${msgId}`, isStar ? '0' : '1');
  // Update bubble
  const el = document.querySelector(`[data-msg-id="${msgId}"]`);
  if (el) {
    el.classList.toggle('starred', !isStar);
    const badge = el.querySelector('.star-badge');
    if (!isStar && !badge) {
      const b = document.createElement('span');
      b.className = 'star-badge';
      b.title = 'Starred';
      b.textContent = '⭐';
      el.querySelector('.bubble')?.appendChild(b);
    } else if (isStar && badge) {
      badge.remove();
    }
  }
  showToast(isStar ? 'Unstarred' : '⭐ Starred');
}

async function deleteMessage(msgId) {
  if (!msgId) return;
  const el = document.querySelector(`[data-msg-id="${msgId}"]`);
  if (!el) return;
  el.style.opacity = '0.3';
  try {
    await apiFetch(`/chat/messages/${msgId}`, { method: 'DELETE' });
    el.style.transition = 'all 0.25s';
    el.style.height = el.offsetHeight + 'px';
    requestAnimationFrame(() => {
      el.style.height  = '0';
      el.style.padding = '0';
      el.style.margin  = '0';
      el.style.opacity = '0';
    });
    setTimeout(() => el.remove(), 280);
    showToast('Message deleted');
  } catch {
    el.style.opacity = '1';
    showToast('Delete failed — endpoint may need adding', true);
  }
}

function confirmClearMessages(customerId, phone) {
  showConfirmModal(
    `Clear all messages with ${phone}?`,
    'This cannot be undone.',
    async () => {
      try {
        await apiFetch(`/chat/clear/${customerId}`, { method: 'DELETE' });
        showToast('Conversation cleared');
        if (customerId === currentCustomerId) {
          const mc = document.getElementById('chat-messages');
          if (mc) { mc.innerHTML = '<div class="msgs-empty">No messages yet — say hello! 👋</div>'; }
        }
        loadConversations(false).catch(() => {});
      } catch {
        showToast('Clear failed — endpoint may need adding', true);
      }
    }
  );
}

/* ── REPLY QUOTE ────────────────────────────────────────── */
function replyQuote(text) {
  const input = document.getElementById('send-input');
  if (!input) return;
  const quoted = safeText(text).split('\n').slice(0,2).join(' ');
  const trimmed = quoted.length > 60 ? quoted.slice(0,57) + '…' : quoted;
  input.value = `> ${trimmed}\n\n` + input.value;
  autoResize(input);
  input.focus();
  // Move cursor to end
  input.setSelectionRange(input.value.length, input.value.length);
}

/* ── IN-CHAT MESSAGE SEARCH ─────────────────────────────── */
function toggleMsgSearch() {
  msgSearchOpen = !msgSearchOpen;
  const bar = document.getElementById('msg-search-bar');
  if (bar) {
    bar.classList.toggle('visible', msgSearchOpen);
    if (msgSearchOpen) document.getElementById('msg-search-input')?.focus();
    else clearMsgSearch();
  }
}

function closeMsgSearch() {
  msgSearchOpen = false;
  document.getElementById('msg-search-bar')?.classList.remove('visible');
  clearMsgSearch();
}

function doMsgSearch(val) {
  const q = val.trim().toLowerCase();
  const bubbles = document.querySelectorAll('#chat-messages .msg .bubble');
  let matchCount = 0;
  bubbles.forEach(b => {
    const txt = b.textContent.toLowerCase();
    const match = q && txt.includes(q);
    b.closest('.msg').style.opacity = q ? (match ? '1' : '0.2') : '1';
    if (match) matchCount++;
  });
  const counter = document.getElementById('msg-search-count');
  if (counter) counter.textContent = q ? `${matchCount} match${matchCount !== 1 ? 'es' : ''}` : '';
}

function clearMsgSearch() {
  const input = document.getElementById('msg-search-input');
  if (input) input.value = '';
  document.querySelectorAll('#chat-messages .msg').forEach(m => { m.style.opacity = '1'; });
  const counter = document.getElementById('msg-search-count');
  if (counter) counter.textContent = '';
}

/* ── INFO PANEL ─────────────────────────────────────────── */
function toggleInfoPanel() {
  infoPanelOpen = !infoPanelOpen;
  document.getElementById('info-panel')?.classList.toggle('open', infoPanelOpen);
  document.getElementById('chat-messages')?.classList.toggle('panel-open', infoPanelOpen);
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

  // Optimistic bubble
  appendBubble({
    id: null, text, direction: 'outgoing',
    status: 'sent', created_at: new Date().toISOString(),
  });
  scrollToBottom();
  showTyping(900);

  try {
    const res = await apiFetch('/chat/send', {
      method: 'POST',
      body: JSON.stringify({ customer_id: currentCustomerId, text }),
    });
    if (res?.whatsapp_result?.error) showToast('⚠ Saved but WhatsApp delivery may have failed', true);
  } catch (e) {
    showToast('Send failed: ' + e.message, true);
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

function handleSendKey(e) {
  if (!e) return;
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage().catch(() => {}); }
}

function autoResize(el) {
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}

/* ── QUICK EMOJI TOOLBAR ────────────────────────────────── */
const QUICK_REPLIES = ['👍', '✅', '😊', '🙏', '🔥', 'OK', 'Hello!', 'Thank you!'];
function insertQuickReply(val) {
  const input = document.getElementById('send-input');
  if (!input) return;
  input.value += val;
  autoResize(input);
  input.focus();
}

/* ── READ TRACKING ──────────────────────────────────────── */
async function markRead() {
  if (!currentCustomerId) return;
  try {
    await apiFetch(`/chat/read/${currentCustomerId}`, { method: 'POST' });
    const conv = allConversations.find(c => c.customer_id === currentCustomerId);
    if (conv) { conv.unread_count = 0; renderContacts(allConversations); }
  } catch {}
}

/* ── TYPING INDICATOR ───────────────────────────────────── */
function showTyping(ms) {
  const el = document.getElementById('typing-indicator');
  if (!el) return;
  el.classList.add('visible');
  scrollToBottom();
  setTimeout(() => el?.classList.remove('visible'), ms);
}

/* ── SCROLL ─────────────────────────────────────────────── */
function scrollToBottom() {
  const c = document.getElementById('chat-messages');
  if (!c) return;
  requestAnimationFrame(() => { c.scrollTop = c.scrollHeight; });
}
function scrollToTop() {
  const c = document.getElementById('chat-messages');
  if (c) c.scrollTop = 0;
}

/* ── CONFIRM MODAL ──────────────────────────────────────── */
function showConfirmModal(title, subtitle, onConfirm) {
  document.getElementById('confirm-modal')?.remove();
  const modal = document.createElement('div');
  modal.id = 'confirm-modal';
  modal.className = 'modal-overlay';
  modal.innerHTML = `
    <div class="modal-box">
      <div class="modal-title">${escHtml(title)}</div>
      <div class="modal-sub">${escHtml(subtitle)}</div>
      <div class="modal-actions">
        <button class="modal-btn modal-cancel" onclick="document.getElementById('confirm-modal').remove()">Cancel</button>
        <button class="modal-btn modal-confirm" id="modal-ok">Confirm</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  document.getElementById('modal-ok').onclick = () => { modal.remove(); onConfirm(); };
}

/* ── UTILS ──────────────────────────────────────────────── */
function safeText(val) { return (val === null || val === undefined) ? '' : String(val); }

function escHtml(s) {
  return safeText(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Escape for inline JS string attrs
function esc(s) {
  return safeText(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\n/g,'\\n');
}

function parseDate(iso) {
  if (!iso) return null;
  let s = String(iso).trim().replace(' ', 'T');
  // Append Z if no timezone indicator
  if (!/[Z+\-]\d*$/.test(s.slice(10))) s += 'Z';
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

function formatTime(iso) {
  const d = parseDate(iso);
  if (!d) return '—';
  const now  = new Date();
  const diff = now - d;
  if (diff < 0)        return d.toLocaleTimeString('en-GB', { hour:'2-digit', minute:'2-digit' });
  if (diff < 60000)    return 'just now';
  if (diff < 3600000)  return Math.floor(diff / 60000) + 'm ago';
  if (diff < 86400000) return d.toLocaleTimeString('en-GB', { hour:'2-digit', minute:'2-digit' });
  if (diff < 604800000)return d.toLocaleDateString('en-GB', { weekday:'short', hour:'2-digit', minute:'2-digit' });
  return d.toLocaleDateString('en-GB', { day:'2-digit', month:'short', year:'numeric' });
}

function fullTimestamp(iso) {
  const d = parseDate(iso);
  if (!d) return '';
  return d.toLocaleString('en-GB', {
    day:'2-digit', month:'short', year:'numeric',
    hour:'2-digit', minute:'2-digit', second:'2-digit'
  });
}

function copyToClipboard(text, successMsg = 'Copied') {
  navigator.clipboard?.writeText(text)
    .then(() => showToast(successMsg))
    .catch(() => showToast('Copy failed', true));
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
  toastTimer = setTimeout(() => t?.classList.add('hidden'), 3200);
}

/* ── INIT ───────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  initTheme();

  document.getElementById('theme-toggle')?.addEventListener('click', toggleTheme);
  document.getElementById('hamburger')?.addEventListener('click', toggleSidebar);
  document.getElementById('sidebar-overlay')?.addEventListener('click', closeSidebar);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      if (sidebarOpen) closeSidebar();
      if (infoPanelOpen) toggleInfoPanel();
      if (msgSearchOpen) closeMsgSearch();
      closeAllMenus();
    }
    // Ctrl/Cmd+F to search in chat
    if ((e.ctrlKey || e.metaKey) && e.key === 'f' && currentCustomerId) {
      e.preventDefault();
      toggleMsgSearch();
    }
  });

  try { connectWS(); } catch (e) { console.warn('WS init failed:', e); }

  loadConversations(true).catch(e => console.warn('Initial load failed:', e));

  // Sidebar refresh every 20s
  setInterval(() => loadConversations(false).catch(() => {}), 20000);
});
