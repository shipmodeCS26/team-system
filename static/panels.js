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

if (typeof module !== "undefined") module.exports = { makePanelLoader, resetPanels };
