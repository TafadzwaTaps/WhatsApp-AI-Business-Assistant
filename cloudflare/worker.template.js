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
const MAINTENANCE_HTML = __MAINTENANCE_HTML__;

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
