var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// worker.js
var KV_KEY = "host_status";
function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" }
  });
}
__name(json, "json");
async function getStatus(env) {
  const raw = await env.HOST_KV.get(KV_KEY);
  if (!raw) {
    return { hosting: false, host_name: null, game_id: null, since: null, save_version: 0, save_key: null };
  }
  return JSON.parse(raw);
}
__name(getStatus, "getStatus");
async function setStatus(env, status) {
  await env.HOST_KV.put(KV_KEY, JSON.stringify(status));
}
__name(setStatus, "setStatus");
var worker_default = {
  async fetch(request, env) {
    const url = new URL(request.url);
    const auth = request.headers.get("X-Auth");
    if (!auth || auth !== env.SHARED_SECRET) {
      return json({ error: "unauthorized" }, 401);
    }
    if (request.method === "GET" && url.pathname === "/status") {
      const status = await getStatus(env);
      return json(status);
    }
    if (request.method === "POST" && url.pathname === "/claim") {
      const body = await request.json().catch(() => ({}));
      const name = (body.name || "unknown").toString().slice(0, 64);
      const gameId = (body.game_id || "unknown").toString().slice(0, 64);
      const current = await getStatus(env);
      if (current.hosting) {
        return json({ ok: false, reason: "already_hosting", current }, 409);
      }
      const next = {
        hosting: true,
        host_name: name,
        game_id: gameId,
        since: Date.now(),
        save_version: current.save_version || 0,
        save_key: current.save_key || null,
        join_code: null
      };
      await setStatus(env, next);
      return json({ ok: true, current: next });
    }
    if (request.method === "POST" && url.pathname === "/release") {
      const body = await request.json().catch(() => ({}));
      const name = (body.name || "unknown").toString().slice(0, 64);
      const saveKey = body.save_key ? body.save_key.toString().slice(0, 256) : null;
      const current = await getStatus(env);
      const next = {
        hosting: false,
        host_name: null,
        // Kept (not cleared) through release, same as save_key below -- lets
        // the dashboard show "last hosted: Zomboid" while idle instead of
        // losing that as soon as the session ends.
        game_id: current.game_id || null,
        since: null,
        // save_version is purely informational (shown in Discord etc) --
        // save_key below is what actually drives sync decisions.
        save_version: (current.save_version || 0) + 1,
        save_key: saveKey || current.save_key || null,
        last_host: name,
        released_at: Date.now()
      };
      await setStatus(env, next);
      return json({ ok: true, current: next });
    }
    if (request.method === "POST" && url.pathname === "/announce_code") {
      const body = await request.json().catch(() => ({}));
      const name = (body.name || "unknown").toString().slice(0, 64);
      const joinCode = (body.join_code || "").toString().slice(0, 32);
      const current = await getStatus(env);
      if (!current.hosting || current.host_name !== name) {
        return json({ ok: false, reason: "not_current_host", current }, 409);
      }
      const next = { ...current, join_code: joinCode };
      await setStatus(env, next);
      return json({ ok: true, current: next });
    }
    return json({ error: "not_found" }, 404);
  }
};
export {
  worker_default as default
};
