// Moonberry coordinator: tracks who's hosting which game, and which save
// each game's world was last uploaded as.
//
// One host per GAME at a time (not one globally): `hosts` maps game_id ->
// that game's live claim. All state lives in a single Durable Object, which
// handles requests one at a time with strongly consistent storage -- with
// several hosts at once, two sessions of different games ending within a
// few seconds would otherwise both rewrite the same record, and on KV
// (eventually consistent, stale reads seen for over a minute) one could
// silently undo the other's save_key.
//
// Old clients (which only know one global host) keep working: the
// top-level hosting/host_name/game_id/save_slot/... fields are still filled
// in -- with the most recently started claim while anyone's hosting -- and
// a /release or /announce_code without a game_id is matched to the
// caller's own claim by player name.
//
// After every change the full state is also copied to the HOST_KV key the
// previous (KV-only) version of this worker used: readable in the
// Cloudflare dashboard, and it lets that version be redeployed as-is. It's
// imported from there the first time this version runs.

import { DurableObject } from "cloudflare:workers";

const KV_KEY = "host_status";
const STATE_KEY = "state";

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" }
  });
}

function emptyState() {
  return {
    hosting: false,
    host_name: null,
    game_id: null,
    since: null,
    // save_key/save_version (singular) are kept for backward compat with
    // clients that predate per-game tracking -- they always reflect
    // whichever game released most recently. save_keys/save_versions/
    // save_owners/save_display_names are the real source of truth, nested
    // { game_id: { slot_id: value } }: a client must always look up ITS
    // OWN game_id and slot in these, never trust the singular fields.
    save_slot: null,
    save_display_name: null,
    save_version: 0,
    save_key: null,
    save_versions: {},
    save_keys: {},
    save_owners: {},
    save_display_names: {},
    join_code: null,
    hosts: {}
  };
}

function str(value, fallback, max) {
  return (value || fallback).toString().slice(0, max);
}

// A state blob written by the old KV-only worker has no `hosts` map --
// its one global claim (if any) becomes that game's entry.
function normalize(state) {
  const s = { ...emptyState(), ...state };
  if (!state.hosts || typeof state.hosts !== "object") {
    s.hosts = {};
    if (state.hosting) {
      s.hosts[state.game_id || "unknown"] = {
        host_name: state.host_name || "unknown",
        save_slot: state.save_slot || "default",
        save_display_name: state.save_display_name || state.save_slot || "default",
        since: state.since || Date.now(),
        join_code: state.join_code || null
      };
    }
  }
  return s;
}

// Refreshes the top-level single-host fields old clients read: the most
// recently started claim while anyone's hosting. While idle, game_id/
// save_slot/save_display_name keep naming the last release (so a
// dashboard can show "last hosted: Zomboid"), as before.
function withLegacyView(state) {
  const entries = Object.entries(state.hosts);
  if (entries.length === 0) {
    return { ...state, hosting: false, host_name: null, since: null, join_code: null };
  }
  const [gameId, host] = entries.reduce((a, b) => (b[1].since > a[1].since ? b : a));
  return {
    ...state,
    hosting: true,
    host_name: host.host_name,
    game_id: gameId,
    save_slot: host.save_slot,
    save_display_name: host.save_display_name,
    since: host.since,
    join_code: host.join_code
  };
}

// The same state, but with the single-host fields describing one specific
// game's claim -- used in a refused /claim's response, so even an old
// client's "Someone is hosting (X)" message names the right person.
function viewForGame(state, gameId) {
  const host = state.hosts[gameId];
  if (!host) return state;
  return {
    ...state,
    hosting: true,
    host_name: host.host_name,
    game_id: gameId,
    save_slot: host.save_slot,
    save_display_name: host.save_display_name,
    since: host.since,
    join_code: host.join_code
  };
}

// Which game's claim a /release or /announce_code is about: the game_id
// the client sent (new clients) if it has a claim, else -- old clients
// send none -- the most recent claim held under the caller's name.
function findClaim(state, name, gameId) {
  if (gameId) return state.hosts[gameId] ? gameId : null;
  const mine = Object.entries(state.hosts).filter(([, h]) => h.host_name === name);
  if (mine.length === 0) return null;
  return mine.reduce((a, b) => (b[1].since > a[1].since ? b : a))[0];
}

export class Coordinator extends DurableObject {
  async load() {
    let state = await this.ctx.storage.get(STATE_KEY);
    if (state === undefined) {
      const raw = await this.env.HOST_KV.get(KV_KEY);
      state = withLegacyView(normalize(raw ? JSON.parse(raw) : emptyState()));
      await this.ctx.storage.put(STATE_KEY, state);
    }
    return state;
  }

  async save(state) {
    const next = withLegacyView(state);
    await this.ctx.storage.put(STATE_KEY, next);
    try {
      await this.env.HOST_KV.put(KV_KEY, JSON.stringify(next));
    } catch (e) {
      // Only the dashboard copy -- the Durable Object is the real record.
      console.error("KV mirror write failed:", e);
    }
    return next;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/status") {
      return json(await this.load());
    }
    if (request.method !== "POST") {
      return json({ error: "not_found" }, 404);
    }
    const body = await request.json().catch(() => ({}));
    // One change at a time, start to finish (including the KV copy), so
    // the copy can never be overwritten by an older state.
    return this.ctx.blockConcurrencyWhile(() => this.handle(url.pathname, body));
  }

  async handle(path, body) {
    const state = await this.load();
    const name = str(body.name, "unknown", 64);

    if (path === "/claim") {
      const gameId = str(body.game_id, "unknown", 64);
      const saveSlot = str(body.save_slot, "default", 128);
      const saveDisplayName = str(body.save_display_name, saveSlot, 128);
      if (state.hosts[gameId]) {
        return json({ ok: false, reason: "already_hosting", current: viewForGame(state, gameId) }, 409);
      }
      const hosts = {
        ...state.hosts,
        [gameId]: { host_name: name, save_slot: saveSlot, save_display_name: saveDisplayName, since: Date.now(), join_code: null }
      };
      const next = await this.save({ ...state, hosts });
      return json({ ok: true, current: viewForGame(next, gameId) });
    }

    if (path === "/release") {
      const saveKey = body.save_key ? body.save_key.toString().slice(0, 256) : null;
      const gameId = findClaim(state, name, body.game_id ? str(body.game_id, "unknown", 64) : null);
      if (gameId === null) {
        // Nothing to release (already released, e.g. a retry). Not an
        // error: releasing is always safe to repeat.
        return json({ ok: true, released: false, current: state });
      }
      // The game and slot this claim was actually FOR -- taken from the
      // live claim, never from the request body, so a client can't write
      // into another claim's slot.
      const claim = state.hosts[gameId];
      const saveSlot = claim.save_slot || "default";
      const saveDisplayName = claim.save_display_name || saveSlot;

      const saveKeys = { ...(state.save_keys || {}) };
      const saveVersions = { ...(state.save_versions || {}) };
      const saveOwners = { ...(state.save_owners || {}) };
      const saveDisplayNames = { ...(state.save_display_names || {}) };
      // A game_id entry from a pre-multi-save client is a bare
      // string/number, not an object keyed by slot -- discard it instead of
      // nesting into it. The singular save_key/save_version fields still
      // carry the same information forward for old clients.
      for (const map of [saveKeys, saveVersions, saveOwners, saveDisplayNames]) {
        if (map[gameId] && typeof map[gameId] !== "object") map[gameId] = {};
      }
      if (saveKey) {
        saveKeys[gameId] = { ...(saveKeys[gameId] || {}), [saveSlot]: saveKey };
        const prevVersion = (saveVersions[gameId] || {})[saveSlot] || 0;
        saveVersions[gameId] = { ...(saveVersions[gameId] || {}), [saveSlot]: prevVersion + 1 };
        saveOwners[gameId] = { ...(saveOwners[gameId] || {}), [saveSlot]: claim.host_name };
        saveDisplayNames[gameId] = { ...(saveDisplayNames[gameId] || {}), [saveSlot]: saveDisplayName };
      }
      const hosts = { ...state.hosts };
      delete hosts[gameId];
      const next = await this.save({
        ...state,
        hosts,
        // Kept through release (overwritten by the legacy view if someone
        // else is still hosting): which game/slot was last hosted.
        game_id: gameId,
        save_slot: saveSlot,
        save_display_name: saveDisplayName,
        save_version: (state.save_version || 0) + 1,
        save_key: saveKey || state.save_key || null,
        save_versions: saveVersions,
        save_keys: saveKeys,
        save_owners: saveOwners,
        save_display_names: saveDisplayNames,
        last_host: claim.host_name,
        released_at: Date.now()
      });
      return json({ ok: true, released: true, game_id: gameId, current: next });
    }

    if (path === "/announce_code") {
      const joinCode = str(body.join_code, "", 32);
      const gameId = findClaim(state, name, body.game_id ? str(body.game_id, "unknown", 64) : null);
      if (gameId === null || state.hosts[gameId].host_name !== name) {
        return json({ ok: false, reason: "not_current_host", current: state }, 409);
      }
      const hosts = { ...state.hosts, [gameId]: { ...state.hosts[gameId], join_code: joinCode } };
      const next = await this.save({ ...state, hosts });
      return json({ ok: true, current: viewForGame(next, gameId) });
    }

    return json({ error: "not_found" }, 404);
  }
}

export default {
  async fetch(request, env) {
    // Checked here, before the Durable Object is even woken up.
    const auth = request.headers.get("X-Auth");
    if (!auth || auth !== env.SHARED_SECRET) {
      return json({ error: "unauthorized" }, 401);
    }
    const stub = env.COORDINATOR.get(env.COORDINATOR.idFromName("main"));
    return stub.fetch(request);
  }
};
