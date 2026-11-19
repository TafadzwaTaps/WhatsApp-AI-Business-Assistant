/* Confirm-email page. The token comes from the link; it is only sent when the person clicks the
   button, so a mail scanner that merely opens the link does not use it up. */
(function () {
  var params = new URLSearchParams(window.location.search);
  var token = params.get('token') || '';
  var btn = document.getElementById('confirm-btn');
  var msg = document.getElementById('msg');
  var dash = document.getElementById('dash-link');

  function show(text, ok) {
    msg.textContent = text;            // textContent only: never markup
    msg.className = 'msg ' + (ok ? 'success' : 'error');
  }

  // Remove the token from the address bar and history as soon as it has been read.
  try { window.history.replaceState({}, document.title, window.location.pathname); } catch (_) {}

  if (!token) {
    btn.style.display = 'none';
    show('This link is incomplete. Open the link from your email again.', false);
    return;
  }

  btn.addEventListener('click', function () {
    btn.disabled = true;
    fetch('/auth/verify-email', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (res.ok) {
          btn.style.display = 'none';
          dash.style.display = 'block';
          show('Your email address is confirmed. Thank you!', true);
        } else {
          btn.disabled = false;
          var d = body && body.detail;
          show(typeof d === 'string' ? d : 'This link is invalid or has expired. Request a new one from your dashboard.', false);
        }
      });
    }).catch(function () {
      btn.disabled = false;
      show('Could not reach the server. Please try again.', false);
    });
  });
})();
