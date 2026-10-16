"use strict";
// Loads one Inventory panel for the selected client. A response is applied only if it is
// still the newest request for that panel AND the client has not changed since it was sent,
// so a slow answer for one client can never show up (or be exported) under another client.
function makePanelLoader(state, api, key, path, render) {
  return async function load() {
    const client = state.client;
    const ticket = state.panelRequests[key] = (state.panelRequests[key] || 0) + 1;
    const current = () => state.panelRequests[key] === ticket && state.client === client;
    try {
      const result = await api(`${path}?client_id=${encodeURIComponent(client)}`);
      if (!current()) return;
      // Defence in depth: keep only rows for the client that was asked for.
      state[key] = (result.clients || []).filter(c => client === "all" || c.client_id === client);
      state[key + "Error"] = null;
    } catch (error) {
      if (!current()) return;
      state[key] = [];
      state[key + "Error"] = error.message;
    }
    render();
  };
}

// On a client switch, drop every panel's data and invalidate requests still in flight.
function resetPanels(state, keys) {
  for (const key of keys) {
    state.panelRequests[key] = (state.panelRequests[key] || 0) + 1;
    state[key] = null;
    state[key + "Error"] = null;
  }
}

// #14: runs the daily Shopify vs. shipped comparison and polls while the server reads Shopify.
// A result is applied only while the same client is selected and no newer check was started.
function makeDailyLoader(state, api, render, wait = ms => new Promise(r => setTimeout(r, ms))) {
  return async function check(day) {
    const client = state.client;
    const ticket = state.panelRequests.daily = (state.panelRequests.daily || 0) + 1;
    const current = () => state.panelRequests.daily === ticket && state.client === client;
    state.daily = { client, day, status: "running", orders_read: 0 };
    render();
    for (let attempt = 0; attempt < 200; attempt++) {
      let res;
      try {
        res = await api(`/api/shopify/daily-orders?client_id=${encodeURIComponent(client)}&date=${encodeURIComponent(day)}`);
      } catch (error) {
        if (current()) { state.daily = { client, day, status: "failed", error: error.message }; render(); }
        return;
      }
      if (!current()) return;
      if (res.status !== "running") {
        // Defence in depth: never show another client's comparison.
        state.daily = res.result && res.result.client_id !== client
          ? { client, day, status: "failed", error: "Unexpected response. Try again." }
          : { client, day, ...res };
        render();
        return;
      }
      state.daily = { client, day, status: "running", orders_read: res.orders_read || 0 };
      render();
      await wait(3000);
      if (!current()) return;
    }
    if (current()) { state.daily = { client, day, status: "failed", error: "Shopify is taking too long. Try again." }; render(); }
  };
}

if (typeof module !== "undefined") module.exports = { makePanelLoader, resetPanels, makeDailyLoader };
