/* Moved out of forgot-password.html (Phase 5 CSP migration): behaviour unchanged. */
const API = '';  // same origin

// ── Submit ─────────────────────────────────────────────
// Phase 1 security fix (2026-09-24): now calls the secure, token-based
// POST /auth/forgot-password (which emails a single-use reset link)
// instead of the retired POST /auth/reset-password-direct.
async function doReset() {
  const identifier = document.getElementById('identifier').value.trim();
  const btn        = document.getElementById('submit-btn');
  const spinner    = document.getElementById('spinner');

  hideMsg();

  if (!identifier) {
    showMsg('Please enter your email address.', 'error');
    document.getElementById('identifier').focus();
    return;
  }

  btn.disabled = true;
  spinner.style.display = 'inline-block';

  try {
    const resp = await fetch('/auth/forgot-password', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ email: identifier }),
    });
    const data = await resp.json();

    if (resp.ok && data.ok) {
      document.getElementById('view-form').style.display = 'none';
      document.getElementById('view-done').style.display = 'block';
    } else {
      showMsg(data.detail || data.error || data.message || 'Something went wrong. Please try again.', 'error');
    }
  } catch (e) {
    showMsg('Network error. Please check your connection and try again.', 'error');
  } finally {
    btn.disabled = false;
    spinner.style.display = 'none';
  }
}

function showMsg(text, type) {
  const el = document.getElementById('msg');
  el.textContent  = text;
  el.className    = 'msg ' + type;
  el.style.display = 'block';
}
function hideMsg() {
  const el = document.getElementById('msg');
  el.style.display = 'none';
}

document.addEventListener('keydown', e => {
  if (e.key === 'Enter') doReset();
});

wzActions.register('doReset', function () { doReset(); });
