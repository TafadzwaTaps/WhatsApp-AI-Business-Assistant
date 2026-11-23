/**
 * WaziBot — Cloudflare Worker: show the branded maintenance page during an outage.
 *
 * Sits in front of Render. Normal traffic passes straight through. When the origin
 * is down (Render's 502/503/504 gateway page, Cloudflare 52x errors, or the origin
 * can't be reached) and the visitor is loading a PAGE, they get the maintenance
 * page instead. API calls, webhooks, WebSockets and assets are passed through
 * untouched so the dashboard can show its own message and Meta/Stripe retry normally.
 *
 * GENERATED FILE: edit worker.template.js / static/maintenance.html, then run
 *   python cloudflare/build_worker.py
 *
 * Optional Worker secret: EDGE_SHARED_SECRET (same value as the Render env var of
 * the same name) so the app can trust the real visitor IP behind Cloudflare.
 */
const MAINTENANCE_HTML = "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"UTF-8\"/>\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><link rel=\"icon\" type=\"image/png\" sizes=\"32x32\" href=\"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAEwklEQVR42sWXy24cVRCGvzqXHs/FY8dG4CSgJEQRCSwiBWXFlgVLEOIBsorCA/AMsEMCCVigILFECFggJASs2CCyCAnBEEAoF+GQ2I5nPDPt6T7nFIue2LnYY4NipXbT1er656+qv6oEEEAB6lOtV0XlDHASYXLkeximKKvAjyr6Xt7pfTp6LgIYwNanWx8YzCkFUGVXTAQBEulsvtI7DUQLaL3d+tAYc0pV451XdwcBCUjGmBMu80+FYfG51KdaLxsxn6lqAOwuBt9IB0QRcUnTK9bXau8jcmAU2LD7JuvZV/ZKoz3ZQ2jyKEzpm0cWvOKi6cYSJbKRtfs7Q2SjWrb166jRHzS3ZfCQ0DJVPwSkZu9lbxgg6UZ7beqv0Ik34MymIB4EYITUK2m9doT6iwfQkAh/rtB55zySGUDQYaB95jjZs7OgSvn7Cp137/ZH2q8fJzs6A9aQf3OV/ieXkZbfAL0lAAFSQovExAv7SHnAH5lm9eN5dLUAATOZUX/pIG6uCQru8MjfK0AU0/Y0XjqIfbyB1B2Dr/5CU1rP6D3/dzOpkMxRXLhFWOij3QLT9PhDU2iR0DLiDrQx7YzUGZI6w5G/jZZV2tyBKWSyRuoWhIU+xYVFJLOjlGwHQBXJDPGfAXGhDzWL1Cz+2AykBEHxz+zBND06jGgRkYbDH52BmCAk/LEZTN2CN8QbA+KNfgVgE4nfXHhsVQfl5dtIZtCgZM/NgjUgkB2bRSYcxcVFiouLyISrABoDRsiem0GjIpmlvHyb1CvBbi6wZpxKFJeWEBG0iPjD05hJjzQc7sg0AMVPtyjO3wQFf3gaaXmk4fFH9qBFBCOUl5bGDje3lVqLt5S/LZMGVbvZuQZ2ronmEbe/hQ4qhkiKDkrs3ib2iQZExe5tQlQ0D5S/LSPe/kcdSBV94XqPeKOP3ddE6g739BQ6CJg9E8SFHuF6D5ISb+XY/S38oSlIiml6Uq8k3hwQrq1W7bkFC1sroRW0O6T8YwV3sA1JyZ6dRfOAZIZwZZW4vAaqhCtd3KF2VQfrhWwJf6yQOsWm/b99DQhoVMpLS4gVUh6onXic2sk5CKmifxigiBS/LkNI1J5/gtrJOdIgIE4ofllCYxo74N24qS3OUMwvo8MIIWH3T1Y5X4sU80vrs6KcXyblEfdUq+qEMqE2Uc4vI1tI8PYMqCI1S7jaJd4cVC1YRlAldQvKyyuVvntD+fttUmdYBSojWCEu5oQr3S37fwcAAGdIy2uEq6vY2Qmk4TGzE8SlnPh3D/EG8Ya40CfdHGAeqyN1j52tE66vEhdz8OMZcNsvDUr+3TWk4UndIWYyY/jDDXQtIA2/PvkGX19hol+SVgtMO2Pt22ubSu8DpdaYmtTtFigtUiWzIhWdRipq78ZZxKrS77xjzaj9xgNwO1khJTMg9t703JdXqbl7q113tt47drrHbvexMVvPODMo/Ue2Eyp9o3AOEWVHJfMQDxQRVThnEH17tD4m/heJ/+swSQKC6Ns2DItfXeYPGmtOoOss7OZppmLEpZQ+yru9tyxgwrD40k34J42YE4js3mkmIiJiEuls3umdZnQLAsSwVnzhav5nUWaBxxCyh36eK98r+kbe6b15p+b+BRrtUpSGr1Y1AAAAAElFTkSuQmCC\"/>\n\n<meta name=\"robots\" content=\"noindex\"/>\n<title>We'll be right back \u2014 WaziBot</title>\n<!-- Fully self-contained (no external CSS/JS/fonts) so it still renders when\n     everything else is down. Safe to paste into any host's custom error page. -->\n<style>\n  :root{--bg:#0a0f0d;--card:#111a15;--line:#1f3025;--text:#e8f5e9;--muted:#8fb097;--green:#22c55e;--amber:#f59e0b}\n  *{box-sizing:border-box}\n  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;\n       background:var(--bg);color:var(--text);font-family:'Helvetica Neue',Arial,sans-serif}\n  .card{max-width:520px;width:100%;background:var(--card);border:1px solid var(--line);border-radius:16px;\n        padding:40px 32px;text-align:center}\n  .logo{font-size:22px;font-weight:800;color:var(--green);margin-bottom:28px}\n  .gear{font-size:44px;margin-bottom:8px;display:inline-block;animation:spin 6s linear infinite}\n  @media (prefers-reduced-motion:reduce){.gear{animation:none}}\n  @keyframes spin{to{transform:rotate(360deg)}}\n  h1{font-size:24px;margin:8px 0 12px}\n  p{color:var(--muted);line-height:1.65;font-size:15px;margin:0 0 14px}\n  .status{display:inline-flex;align-items:center;gap:8px;margin-top:10px;padding:8px 14px;border:1px solid var(--line);\n          border-radius:999px;font-size:13px;color:var(--text)}\n  .dot{width:9px;height:9px;border-radius:50%;background:var(--amber)}\n  .ok .dot{background:var(--green)}\n  button{margin-top:22px;background:var(--green);color:#000;border:0;border-radius:9px;padding:12px 26px;\n         font-weight:800;font-size:14px;cursor:pointer}\n  button:focus-visible{outline:2px solid var(--text);outline-offset:2px}\n  .small{font-size:12px;margin-top:22px;opacity:.75}\n</style>\n</head>\n<body>\n<main class=\"card\" role=\"main\">\n  <div class=\"logo\">WaziBot</div>\n  <div class=\"gear\" aria-hidden=\"true\">\u2699\ufe0f</div>\n  <h1>We're experiencing issues</h1>\n  <p>Our servers are temporarily unavailable. Our technicians have been alerted and are working on it.</p>\n  <p>Your data is safe. Please try again in a few minutes \u2014 this page will reconnect automatically.</p>\n  <div class=\"status\" id=\"status\" role=\"status\" aria-live=\"polite\"><span class=\"dot\"></span><span id=\"statusText\">Checking connection\u2026</span></div>\n  <div><button type=\"button\" id=\"retry\">Try again now</button></div>\n  <p class=\"small\">Customers: your order or message is not lost \u2014 simply resend it shortly.</p>\n</main>\n<script>\n(function(){\n  var box=document.getElementById('status'), txt=document.getElementById('statusText'), tries=0, timer;\n  function back(){\n    // When opened directly as /static/maintenance.html go home; when served in place\n    // of a failed page, reload that same URL.\n    if (location.pathname.indexOf('maintenance.html') !== -1) location.href='/'; else location.reload();\n  }\n  function check(manual){\n    txt.textContent = manual ? 'Checking\u2026' : txt.textContent;\n    var c = new AbortController(); var t = setTimeout(function(){c.abort()}, 6000);\n    fetch('/healthz',{cache:'no-store',signal:c.signal}).then(function(r){\n      clearTimeout(t);\n      if (r.ok) { box.className='status ok'; txt.textContent='Back online \u2014 reconnecting\u2026'; setTimeout(back,600); }\n      else fail();\n    }).catch(function(){ clearTimeout(t); fail(); });\n  }\n  function fail(){\n    tries++; box.className='status';\n    var wait = Math.min(15 + tries*5, 60), left = wait;\n    txt.textContent='Still working on it \u2014 retrying in '+left+'s';\n    clearInterval(timer);\n    timer=setInterval(function(){\n      left--; if(left<=0){clearInterval(timer);check();} else txt.textContent='Still working on it \u2014 retrying in '+left+'s';\n    },1000);\n  }\n  document.getElementById('retry').addEventListener('click',function(){clearInterval(timer);check(true);});\n  check();\n})();\n</script>\n</body>\n</html>\n";

// Statuses that mean "the server is down" (not "the app deliberately said so").
const OUTAGE_STATUSES = new Set([502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530]);

function wantsHtmlPage(request) {
  if (request.method !== "GET" && request.method !== "HEAD") return false;
  if ((request.headers.get("Upgrade") || "").toLowerCase() === "websocket") return false;
  return (request.headers.get("Accept") || "").includes("text/html");
}

function maintenancePage() {
  return new Response(MAINTENANCE_HTML, {
    status: 503,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "Retry-After": "30",
      "X-Robots-Tag": "noindex",
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Preview the page any time:  https://your-domain/?wz_maintenance_test=1
    if (url.searchParams.get("wz_maintenance_test") === "1") return maintenancePage();

    // Never let a client supply the edge secret; add ours if configured.
    const headers = new Headers(request.headers);
    headers.delete("X-WZ-Edge");
    if (env && env.EDGE_SHARED_SECRET) headers.set("X-WZ-Edge", env.EDGE_SHARED_SECRET);

    // redirect:"manual" => the browser follows the app's redirects, not the Worker.
    const originRequest = new Request(request, { headers, redirect: "manual" });

    let response;
    try {
      response = await fetch(originRequest);
    } catch (err) {
      if (wantsHtmlPage(request)) return maintenancePage();
      return new Response(JSON.stringify({ detail: "Service temporarily unavailable" }), {
        status: 503,
        headers: { "Content-Type": "application/json", "Retry-After": "30" },
      });
    }

    // X-WZ-App is added by the app to everything it serves itself; if it is
    // missing on an outage status, the response came from the gateway.
    if (OUTAGE_STATUSES.has(response.status) && !response.headers.has("X-WZ-App") && wantsHtmlPage(request)) {
      return maintenancePage();
    }
    return response;
  },
};
