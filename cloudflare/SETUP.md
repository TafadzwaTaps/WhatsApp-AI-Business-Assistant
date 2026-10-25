# Connect Cloudflare so visitors see the maintenance page during an outage

Result: normal traffic goes through Cloudflare to Render unchanged. If Render is down
(crash, restart, out of memory), people loading a page see the WaziBot "We're experiencing
issues - technicians are working on it" page instead of Render's grey "502 Bad Gateway".
Cost: free (Cloudflare Free plan + a free Worker).

Do these in order. Total time about 30 minutes, plus waiting for DNS.

## A. Put the domain on Cloudflare

1. Cloudflare dashboard -> **Add a domain** -> `wazibothq.com` -> choose the **Free** plan.
2. Cloudflare imports your existing DNS records. Compare them with Render
   (Render -> your service -> Settings -> Custom Domains shows what is required):
   - the root domain `wazibothq.com` and `www` must point to Render exactly as Render says;
   - **keep every email record** (MX, SPF/TXT, DKIM, DMARC - for Resend or any mailbox);
   - set the website records (root + `www`) to **Proxied** (orange cloud);
   - set email records to **DNS only** (grey cloud). Mail records can never be proxied.
3. At your domain registrar, replace the nameservers with the two Cloudflare gives you.
   Wait until Cloudflare shows the site as **Active** (minutes to a few hours).
4. Cloudflare -> **SSL/TLS -> Overview** -> set the mode to **Full (strict)**.
   (Not "Flexible" - that causes endless redirects with Render.)
5. Cloudflare -> **Security -> Bots** -> make sure **Bot Fight Mode is OFF** for now.
   It can challenge Meta (WhatsApp) and Stripe webhooks, which would silently break orders.
6. Cloudflare -> **Caching -> Cache Rules** -> Create rule: if Hostname equals `wazibothq.com`
   (and `www.wazibothq.com`) -> **Bypass cache**. Without this Cloudflare can keep serving an
   old `dashboard.js` for hours after you deploy.

## B. Add the Worker (this is what shows the maintenance page)

7. Generate a secret: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
8. Cloudflare -> **Workers & Pages -> Create -> Create Worker** -> name it `wazibot-maintenance`
   -> Deploy -> **Edit code** -> delete the sample and paste the whole contents of
   `cloudflare/maintenance-worker.js` -> **Deploy**.
9. Worker -> **Settings -> Variables and Secrets -> Add** -> type **Secret**, name
   `EDGE_SHARED_SECRET`, value = the secret from step 7 -> Deploy.
10. Render -> your service -> **Environment** -> add `EDGE_SHARED_SECRET` = the same value ->
    save (Render redeploys). This lets the app see each visitor's real IP behind Cloudflare so
    login lockouts and rate limits stay per-person instead of everyone sharing Cloudflare's IPs.
    (Deploy this code to Render BEFORE or together with this step.)
11. Worker -> **Settings -> Domains & Routes -> Add -> Route**:
    `wazibothq.com/*` (zone `wazibothq.com`), then again for `www.wazibothq.com/*`.
    If asked for a failure mode choose **Fail open** (if the Worker ever fails, traffic continues).

## C. Test it

12. Open `https://wazibothq.com/?wz_maintenance_test=1` -> you should see the maintenance page.
13. Check normal use still works: log in, open the Live Inbox (live updates), send a test WhatsApp
    message, place a test order.
14. Real outage test (do it at a quiet time): Render -> your service -> **Suspend**. Wait a minute,
    load `https://wazibothq.com` in a private window -> maintenance page. Then **Resume**; the
    page reconnects by itself when the server is back.

## Notes
- Only page loads get the maintenance page. Dashboard API calls, webhooks and WebSockets are passed
  through unchanged (the dashboard shows its own amber "system experiencing issues" banner).
- Free Workers allow 100,000 requests/day. Check Cloudflare -> Workers -> Metrics now and then;
  every page, asset and API call counts.
- To change the page: edit `static/maintenance.html`, run `python cloudflare/build_worker.py`,
  paste the new `maintenance-worker.js` into the Worker, Deploy.
- Undo at any time: Worker -> Domains & Routes -> delete the routes (instant), or set the DNS
  records back to DNS only (grey cloud).
- Also still worth doing in Render: Settings -> Health Check Path = `/healthz`.
