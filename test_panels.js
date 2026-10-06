"use strict";
// Regression tests for PR #15 review: a slow response for one client must never
// replace the panel (or CSV export) of a client selected later.
const test = require("node:test");
const assert = require("node:assert");
const { makePanelLoader, resetPanels } = require("./static/panels.js");

function deferredApi() {
  const pending = [];
  const api = url => new Promise((resolve, reject) => pending.push({ url, resolve, reject }));
  return { api, pending };
}
const body = client => ({ clients: [{ client_id: client, variants: [client + "-row"] }] });
const flush = () => new Promise(r => setImmediate(r));

for (const key of ["shopify", "ssk", "calculated"]) {
  test(`${key}: out-of-order responses after a client switch keep the selected client`, async () => {
    const state = { client: "muravai", panelRequests: {} };
    const { api, pending } = deferredApi();
    let renders = 0;
    const load = makePanelLoader(state, api, key, "/api/x", () => renders++);
    load();                                   // Muravai request
    state.client = "puravita";
    resetPanels(state, [key]);                // what the client dropdown does
    load();                                   // PuraVita request
    pending[1].resolve(body("puravita"));     // PuraVita answers first...
    await flush();
    pending[0].resolve(body("muravai"));      // ...Muravai answers last
    await flush();
    assert.deepStrictEqual(state[key].map(c => c.client_id), ["puravita"]);
    assert.strictEqual(renders, 1);
    assert.match(pending[0].url, /client_id=muravai$/);
  });
}

test("a stale failure does not overwrite newer data", async () => {
  const state = { client: "muravai", panelRequests: {} };
  const { api, pending } = deferredApi();
  const load = makePanelLoader(state, api, "shopify", "/api/x", () => {});
  load();
  load();                                     // repeat refresh, same client
  pending[1].resolve(body("muravai"));
  await flush();
  pending[0].reject(new Error("timeout"));
  await flush();
  assert.strictEqual(state.shopifyError, null);
  assert.deepStrictEqual(state.shopify.map(c => c.client_id), ["muravai"]);
});

test("rows for another client in a response are dropped", async () => {
  const state = { client: "muravai", panelRequests: {} };
  const load = makePanelLoader(state, async () => ({
    clients: [{ client_id: "muravai" }, { client_id: "onset" }] }), "ssk", "/api/x", () => {});
  await load();
  assert.deepStrictEqual(state.ssk.map(c => c.client_id), ["muravai"]);
});

test("switching clients clears old data immediately", () => {
  const state = { client: "puravita", panelRequests: {}, shopify: [{ client_id: "muravai" }], shopifyError: "x" };
  resetPanels(state, ["shopify"]);
  assert.strictEqual(state.shopify, null);
  assert.strictEqual(state.shopifyError, null);
});
