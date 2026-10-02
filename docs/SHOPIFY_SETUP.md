# Connecting a client's Shopify store (read-only)

ShipMode **only reads** Shopify. It never edits, cancels, fulfills, tags, or
adds notes to orders, products, or inventory. The app enforces this three ways:

1. The client grants the ShipMode app **read scopes only**. Shopify itself
   rejects anything a read-only token tries to change.
2. On every connection, ShipMode checks the token's granted scopes and
   **refuses to use a token that has any write scope** (`write_scope_granted`).
3. The code can only send a fixed list of read queries; anything else is
   blocked before it leaves the server (`shopify_source.py`, tested in `test_shopify.py`).

## What the client does (about 10 minutes, store owner)

Since January 1, 2026 Shopify no longer lets stores create new "custom apps" in
the admin. New apps are made in the **Shopify Dev Dashboard** and give a
**Client ID + Client secret**, not a permanent token. ShipMode exchanges them
for a read token that Shopify expires every 24 hours; the app renews it
automatically. Screen names below may differ slightly; confirm against
Shopify's help center before sending.

1. The **store owner** signs in to the Shopify Dev Dashboard with the same
   Shopify account/organization that owns the store. (The client-credentials
   method only works when the app and the store belong to the same organization.)
2. Create an app named **ShipMode (read-only)**.
3. In the app's configuration/version, select these Admin API access scopes
   and **no others**:
   - `read_products`: SKU mapping check (Issue #12, required)
   - `read_inventory`: inventory comparison (later Issues)
   - `read_orders`: No Movement and daily order checks (Issues #13, #14)
   - `read_customers`: reship address check (Issue #13)
   Shipping addresses and names are also "protected customer data". Shopify may
   ask the app to request that access level separately.
4. Release the version, then **install the app on the store**.
5. Send ShipMode, through a password-manager share (never Slack, email, or chat
   in plain text): the **Client ID**, the **Client secret**, and the store's
   `.myshopify.com` domain (not the public website domain).

If the store already has an older admin-created custom app for ShipMode, its
Admin API token (`shpat_…`) still works and can be sent instead.

If Shopify refuses the client-credentials exchange for this store (for example
`shop_not_permitted`), the fallback is a ShipMode-owned app the client installs
by link (OAuth). That needs an install/callback route that is **not built yet**
and would be its own Issue.

## What ShipMode does (Render private environment, never Git)

```
SHOPIFY_ENABLED=true
SHOPIFY_STORES_JSON={"muravai": {"shop": "<store>.myshopify.com", "client_id": "…", "client_secret": "…"}}
```

or, for an older admin-created app with a fixed token:

```
SHOPIFY_STORES_JSON={"muravai": {"shop": "<store>.myshopify.com", "token": "<shpat_…>"}}
```

Keys are the existing client IDs: `claritymd`, `fascial-labs`, `muravai`,
`nuerosmile`, `puravita`, `onset`. `WORKSPACE_USER`, `WORKSPACE_PASSWORD_HASH`
and `SECRET_KEY` must also be set: when Shopify is enabled the whole
workspace requires sign-in, and it fails closed without them.

Then open **Inventory → Shopify SKU mapping** with the client selected.

## Error codes (per client; other clients keep working)

| Code | Meaning | Fix |
|---|---|---|
| `not_configured` | No valid store entry for this client | Check `SHOPIFY_STORES_JSON` (domain must end in `.myshopify.com`) |
| `access_denied` | Token or client credentials rejected | App uninstalled, token revoked, or wrong secret |
| `missing_scope` | `read_products` not granted | Client adds the scope and reinstalls |
| `write_scope_granted` | Token can make changes | Client removes every `write_*` scope; ShipMode will not connect until then |
| `not_found` | Store domain wrong | Use the `.myshopify.com` domain |
| `unavailable` | Shopify slow, throttled, or down | Try again later |
| `read_failed` | Anything else | Check server logs (client + code only) |

Logs record the client, error code, and HTTP status only, never the store
domain or credentials.
