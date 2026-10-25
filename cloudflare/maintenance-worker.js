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
const MAINTENANCE_HTML = "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"UTF-8\"/>\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>\n<meta name=\"robots\" content=\"noindex\"/>\n<title>We'll be right back \u2014 WaziBot</title>\n<!-- Fully self-contained (no external CSS/JS/fonts) so it still renders when\n     everything else is down. Safe to paste into any host's custom error page. -->\n<style>\n  :root{--bg:#0a0f0d;--card:#111a15;--line:#1f3025;--text:#e8f5e9;--muted:#8fb097;--green:#22c55e;--amber:#f59e0b}\n  *{box-sizing:border-box}\n  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;\n       background:var(--bg);color:var(--text);font-family:'Helvetica Neue',Arial,sans-serif}\n  .card{max-width:520px;width:100%;background:var(--card);border:1px solid var(--line);border-radius:16px;\n        padding:40px 32px;text-align:center}\n  .logo{font-size:22px;font-weight:800;color:var(--green);margin-bottom:28px}\n  .gear{font-size:44px;margin-bottom:8px;display:inline-block;animation:spin 6s linear infinite}\n  @media (prefers-reduced-motion:reduce){.gear{animation:none}}\n  @keyframes spin{to{transform:rotate(360deg)}}\n  h1{font-size:24px;margin:8px 0 12px}\n  p{color:var(--muted);line-height:1.65;font-size:15px;margin:0 0 14px}\n  .status{display:inline-flex;align-items:center;gap:8px;margin-top:10px;padding:8px 14px;border:1px solid var(--line);\n          border-radius:999px;font-size:13px;color:var(--text)}\n  .dot{width:9px;height:9px;border-radius:50%;background:var(--amber)}\n  .ok .dot{background:var(--green)}\n  button{margin-top:22px;background:var(--green);color:#000;border:0;border-radius:9px;padding:12px 26px;\n         font-weight:800;font-size:14px;cursor:pointer}\n  button:focus-visible{outline:2px solid var(--text);outline-offset:2px}\n  .small{font-size:12px;margin-top:22px;opacity:.75}\n</style>\n</head>\n<body>\n<main class=\"card\" role=\"main\">\n  <div class=\"logo\">WaziBot</div>\n  <div class=\"gear\" aria-hidden=\"true\">\u2699\ufe0f</div>\n  <h1>We're experiencing issues</h1>\n  <p>Our servers are temporarily unavailable. Our technicians have been alerted and are working on it.</p>\n  <p>Your data is safe. Please try again in a few minutes \u2014 this page will reconnect automatically.</p>\n  <div class=\"status\" id=\"status\" role=\"status\" aria-live=\"polite\"><span class=\"dot\"></span><span id=\"statusText\">Checking connection\u2026</span></div>\n  <div><button type=\"button\" id=\"retry\">Try again now</button></div>\n  <p class=\"small\">Customers: your order or message is not lost \u2014 simply resend it shortly.</p>\n</main>\n<script>\n(function(){\n  var box=document.getElementById('status'), txt=document.getElementById('statusText'), tries=0, timer;\n  function back(){\n    // When opened directly as /static/maintenance.html go home; when served in place\n    // of a failed page, reload that same URL.\n    if (location.pathname.indexOf('maintenance.html') !== -1) location.href='/'; else location.reload();\n  }\n  function check(manual){\n    txt.textContent = manual ? 'Checking\u2026' : txt.textContent;\n    var c = new AbortController(); var t = setTimeout(function(){c.abort()}, 6000);\n    fetch('/healthz',{cache:'no-store',signal:c.signal}).then(function(r){\n      clearTimeout(t);\n      if (r.ok) { box.className='status ok'; txt.textContent='Back online \u2014 reconnecting\u2026'; setTimeout(back,600); }\n      else fail();\n    }).catch(function(){ clearTimeout(t); fail(); });\n  }\n  function fail(){\n    tries++; box.className='status';\n    var wait = Math.min(15 + tries*5, 60), left = wait;\n    txt.textContent='Still working on it \u2014 retrying in '+left+'s';\n    clearInterval(timer);\n    timer=setInterval(function(){\n      left--; if(left<=0){clearInterval(timer);check();} else txt.textContent='Still working on it \u2014 retrying in '+left+'s';\n    },1000);\n  }\n  document.getElementById('retry').addEventListener('click',function(){clearInterval(timer);check(true);});\n  check();\n})();\n</script>\n</body>\n</html>\n";

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
