// Cloudflare Worker — WebSocket proxy to Deriv API
// Deploy: cd scripts/deriv_ws_proxy && wrangler deploy

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return new Response("ok");
    }

    // Debug endpoint — test if Worker can reach Deriv
    if (url.pathname === "/test-upstream") {
      const targets = [
        "https://ws.derivws.com",
        "https://ws.binaryws.com",
        "https://green.derivws.com",
        "https://blue.derivws.com",
      ];
      const results = [];
      for (const t of targets) {
        try {
          const r = await fetch(t, { headers: { "User-Agent": "Mozilla/5.0" } });
          results.push(`${t}: ${r.status}`);
        } catch (e) {
          results.push(`${t}: ${e.message}`);
        }
      }
      return new Response(results.join("\n"));
    }

    const upgrade = request.headers.get("Upgrade");
    if (!upgrade || upgrade.toLowerCase() !== "websocket") {
      return new Response("WebSocket proxy for Deriv API. Connect via wss://", { status: 426 });
    }

    // Try multiple upstream endpoints
    const upstreams = [
      `https://ws.derivws.com${url.pathname}${url.search}`,
      `https://ws.binaryws.com${url.pathname}${url.search}`,
      `https://green.derivws.com${url.pathname}${url.search}`,
    ];

    let derivResp = null;
    let derivWs = null;

    for (const target of upstreams) {
      try {
        derivResp = await fetch(target, {
          headers: {
            Upgrade: "websocket",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            Origin: "https://app.deriv.com",
          },
        });
        derivWs = derivResp.webSocket;
        if (derivWs) break;
      } catch (_) {}
    }

    if (!derivWs) {
      return new Response("Could not connect to Deriv upstream", { status: 502 });
    }

    derivWs.accept();

    const [client, server] = Object.values(new WebSocketPair());
    server.accept();

    derivWs.addEventListener("message", (event) => {
      try { server.send(event.data); } catch (_) {}
    });
    derivWs.addEventListener("close", (event) => {
      server.close(event.code || 1000, event.reason || "upstream closed");
    });
    derivWs.addEventListener("error", () => {
      server.close(1011, "upstream error");
    });

    server.addEventListener("message", (event) => {
      try { derivWs.send(event.data); } catch (_) {}
    });
    server.addEventListener("close", (event) => {
      derivWs.close(event.code || 1000, event.reason || "client closed");
    });

    return new Response(null, { status: 101, webSocket: client });
  },
};
