/* Moved out of reset-password.html (Phase 5 CSP migration): behaviour unchanged. */
let _token = '';
// One policy for signup, reset and change: 8 to 128 characters, at most 72 UTF-8 bytes, no
// composition rules (the server also rejects common passwords).
const REQS = {
  len: pw => pw.length >= 8,
  max: pw => pw.length <= 128 && new TextEncoder().encode(pw).length <= 72,
};

// ── Init — extract token from URL and validate ────────────────────────────────
window.addEventListener('DOMContentLoaded', async () => {
  const params = new URLSearchParams(window.location.search);
  _token = params.get('token') || '';
  if (!_token) {
    showView('invalid');
    return;
  }
  try {
    const resp = await fetch('/auth/validate-reset-token', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({token: _token})
    });
    const data = await resp.json();
    if (data.valid) {
      showView('form');
    } else {
      document.getElementById('invalid-reason').textContent =
        data.reason || 'This password reset link is invalid or has expired.';
      showView('invalid');
    }
  } catch(e) {
    document.getElementById('invalid-reason').textContent =
      'Could not validate your link. Please check your connection and try again.';
    showView('invalid');
  }
});

// ── Strength meter ────────────────────────────────────────────────────────────
function onPasswordInput() {
  const pw  = document.getElementById('new-pass').value;
  // Strength by length (guidance only; the requirements above are what is enforced)
  const score = !pw ? 0 : pw.length < 8 ? 1 : pw.length < 12 ? 2 : pw.length < 16 ? 3 : 4;

  // Update requirement checklist
  Object.keys(REQS).forEach(k => {
    const el = document.getElementById('req-' + k);
    if (el) el.classList.toggle('met', REQS[k](pw));
  });

  // Strength bar
  const labels   = ['', 'Too short', 'Fair', 'Good', 'Strong'];
  const classes  = ['', 'weak', 'fair', 'good', 'good'];
  const segments = document.querySelectorAll('.strength-seg');
  segments.forEach((seg, i) => {
    seg.className = 'strength-seg' + (i < score ? ' ' + classes[score] : '');
  });
  document.getElementById('strength-label').textContent = pw ? labels[score] || '' : '';

  // Input border
  const inp = document.getElementById('new-pass');
  inp.className = pw === '' ? '' : (score >= 2 && REQS.max(pw) ? 'valid' : 'invalid');

  updateSubmitBtn();
  onConfirmInput(); // re-validate confirm field
}

function onConfirmInput() {
  const pw  = document.getElementById('new-pass').value;
  const cpw = document.getElementById('confirm-pass').value;
  const el  = document.getElementById('confirm-msg');
  const inp = document.getElementById('confirm-pass');
  if (!cpw) { el.textContent = ''; inp.className = ''; }
  else if (pw === cpw) {
    el.textContent = '✓ Passwords match';
    el.style.color = 'var(--green)';
    inp.className = 'valid';
  } else {
    el.textContent = '✗ Passwords do not match';
    el.style.color = 'var(--red)';
    inp.className = 'invalid';
  }
  updateSubmitBtn();
}

function updateSubmitBtn() {
  const pw  = document.getElementById('new-pass').value;
  const cpw = document.getElementById('confirm-pass').value;
  const allMet = Object.values(REQS).every(fn => fn(pw));
  document.getElementById('submit-btn').disabled = !(allMet && pw === cpw);
}

// ── Submit ────────────────────────────────────────────────────────────────────
async function submitReset() {
  const pw  = document.getElementById('new-pass').value;
  const cpw = document.getElementById('confirm-pass').value;
  const btn = document.getElementById('submit-btn');
  const sp  = document.getElementById('spinner');
  const msg = document.getElementById('msg');

  msg.style.display = 'none';
  btn.disabled = true;
  sp.style.display = 'inline-block';

  try {
    const resp = await fetch('/auth/reset-password', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({token: _token, new_password: pw, confirm_password: cpw})
    });
    const data = await resp.json();
    if (resp.ok && data.ok) {
      showView('success');
      // Auto-redirect after 5 seconds
      let secs = 5;
      const tick = setInterval(() => {
        secs--;
        const el = document.getElementById('redirect-msg');
        if (el) el.textContent = 'Redirecting in ' + secs + ' second' + (secs !== 1 ? 's' : '') + '…';
        if (secs <= 0) { clearInterval(tick); window.location.href = '/static/signup.html'; }
      }, 1000);
    } else {
      const errMsg = data.detail || data.error || data.message || 'Password reset failed. Please try again.';
      msg.textContent = errMsg;
      msg.className   = 'msg error';
      msg.style.display = 'block';
      btn.disabled = false;
    }
  } catch(e) {
    msg.textContent   = 'Network error. Please check your connection and try again.';
    msg.className     = 'msg error';
    msg.style.display = 'block';
    btn.disabled      = false;
  } finally {
    sp.style.display = 'none';
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function showView(name) {
  ['loading','invalid','form','success'].forEach(v => {
    const el = document.getElementById('view-' + v);
    if (el) el.style.display = v === name ? 'block' : 'none';
  });
}

function toggleEye(inputId, btnId) {
  const inp = document.getElementById(inputId);
  const btn = document.getElementById(btnId);
  if (inp.type === 'password') { inp.type = 'text'; btn.textContent = '🙈'; }
  else { inp.type = 'password'; btn.textContent = '👁'; }
}

wzActions.register('onPasswordInput', function () { onPasswordInput(); });
wzActions.register('onConfirmInput', function () { onConfirmInput(); });
wzActions.register('toggleEye', function (ev, el) { toggleEye(el.dataset.target, el.dataset.eye); });
wzActions.register('submitReset', function () { submitReset(); });
