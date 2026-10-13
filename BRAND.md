# ShipMode brand guide (for the Operations app)

Every screen in the ShipMode app uses this logo, palette and type. Do not introduce new brand colors or fonts.

## Logo

- File: `static/shipmode-logo.png` (circle badge, transparent corners).
- Placement: top of the black sidebar on every page, 76px on desktop, 44px on mobile. Links to the home screen.
- Always on the black sidebar (`--sm-black`) or on white. Never stretch, recolor, rotate, or place on the blue.
- Browser tab icon: the same badge.
- The current file is cut from a 308px screenshot. Replace it with an original high-resolution PNG or SVG from ShipMode when available.

## Color palette

Updated 2026-09-29 to match the shipmode-v4 website (Gly's reference): cream page, navy
ink and rules, sky-blue highlight. Hex values were sampled from that design.

| Token | Hex | Use in the app |
|---|---|---|
| `--sm-cream` | `#FAF6EA` | Page background |
| `--sm-cream-deep` | `#F2ECDC` | Table headers, panels |
| `--sm-grid` | `#ECE5D3` | Faint background grid |
| `--sm-navy` | `#031656` | Headings, text, sidebar, rules, primary buttons (pill shaped) |
| `--sm-navy-soft` | `#1E2F6B` | Hover on navy buttons |
| `--sm-sky` | `#9BD0F7` | Highlight behind a key word, active tab in the sidebar, focus ring |
| `--sm-sky-soft` | `#D8ECFB` | Notices and info banners |
| `--sm-white` | `#FFFFFF` | Text on navy, inputs |

Navy means "you can act here" (buttons, links, the selected filter). Sky blue highlights.
Neither ever means a status.

### Status colors (separate from the brand)

Inventory status needs its own colors so it never gets confused with the brand blue:

| Status | Token | Hex |
|---|---|---|
| Healthy / Verified | `--sm-ok` | `#1E8E4E` |
| Warning / Review | `--sm-warn` | `#C27A00` |
| Critical / Blocked | `--sm-critical` | `#C62828` |
| Incomplete / Unknown | `--sm-muted` | `#6B7280` |

Always pair a status color with its word (for example "Review"), never color alone.

## Typography

The logo uses heavy, italic, condensed lettering. The app echoes it with one family:

- Headings and big numbers (On Hand, Coverage): **Barlow Condensed**, ExtraBold 800, italic, for page titles and headline figures only.
- Everything else (tables, forms, labels, body): **Barlow**, 400 / 600, upright.
- Self-hosted in `static/fonts/` (the site security policy blocks outside fonts). Fallback: `"Arial Narrow", Arial, sans-serif`.
- Numbers in tables use tabular figures (`font-variant-numeric: tabular-nums`) so columns line up.

The italic display style is the brand accent. Use it for titles and headline numbers, not for table data or forms.

## Layout format

- Navy sidebar with the logo at the top and the tabs (No Movement, Inventory, Invoices, and later others) below it.
- Cream page with a faint grid, uppercase condensed italic navy headings, navy rules and card borders.
- Buttons and filters are rounded pills: filled navy for the main action, navy outline otherwise.
- Data tables are the main content. Keep them dense and readable; no decorative cards around every figure.
- Responsive down to mobile; tables scroll horizontally inside their own container.

## CSS tokens

```css
:root {
  --sm-black: #07080A;
  --sm-white: #FFFFFF;
  --sm-blue: #2563D6;
  --sm-blue-light: #4775BB;
  --sm-blue-deep: #294785;
  --sm-navy: #1B263C;
  --sm-silver: #D4D7D9;
  --sm-surface: #F5F6F7;

  --sm-ok: #1E8E4E;
  --sm-warn: #C27A00;
  --sm-critical: #C62828;
  --sm-muted: #6B7280;

  --sm-font-display: "Barlow Condensed", "Arial Narrow", Arial, sans-serif;
  --sm-font-body: "Barlow", Arial, sans-serif;
}
```
