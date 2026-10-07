// Cloudflare Worker — WebSocket proxy to Deriv API
// Deploy: npx wrangler deploy
//
// Your bot connects to: wss://YOUR_WORKER.workers.dev/websockets/v3?app_id=YOUR_APP_ID
// The worker forwards it to: wss://ws.derivws.com/websockets/v3?app_id=YOUR_APP_ID

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // Health check
    if (url.pathname === "/health") {
      return new Response("ok");
    }

    // Only handle WebSocket upgrades
    const upgrade = request.headers.get("Upgrade");
    if (!upgrade || upgrade.toLowerCase() !== "websocket") {
      return new Response("WebSocket proxy for Deriv API. Connect via wss://", { status: 426 });
    }

    // Build target URL preserving query params (app_id etc)
    const target = `wss://ws.derivws.com${url.pathname}${url.search}`;

    // Connect to Deriv
    const derivWs = new WebSocket(target);

    // Create client-facing WebSocket pair
    const [client, server] = Object.values(new WebSocketPair());

    // Wire server side to Deriv
    server.accept();

    derivWs.addEventListener("message", (event) => {
      try { server.send(event.data); } catch (_) {}
    });

    derivWs.addEventListener("close", (event) => {
      server.close(event.code, event.reason);
    });

    derivWs.addEventListener("error", () => {
      server.close(1011, "upstream error");
    });

    server.addEventListener("message", (event) => {
      try { derivWs.send(event.data); } catch (_) {}
    });

    server.addEventListener("close", (event) => {
      derivWs.close(event.code, event.reason);
    });

    return new Response(null, { status: 101, webSocket: client });
  },
};
