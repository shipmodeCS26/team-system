# ShipMode brand guide (for the Operations app)

Every screen in the ShipMode app uses this logo, palette and type. Do not introduce new brand colors or fonts.

## Logo

- File: `static/shipmode-logo.png` (circle badge, transparent corners).
- Placement: top of the black sidebar on every page, 76px on desktop, 44px on mobile. Links to the home screen.
- Always on the black sidebar (`--sm-black`) or on white. Never stretch, recolor, rotate, or place on the blue.
- Browser tab icon: the same badge.
- The current file is cut from a 308px screenshot. Replace it with an original high-resolution PNG or SVG from ShipMode when available.

## Color palette

| Token | Hex | Where it comes from in the logo | Use in the app |
|---|---|---|---|
| `--sm-black` | `#07080A` | Badge background | Header bar, logo backdrop |
| `--sm-white` | `#FFFFFF` | Letterforms | Page background, text on dark |
| `--sm-blue` | `#2563D6` | Bright blue extrusion | Primary buttons, links, active tab, focus ring |
| `--sm-blue-light` | `#4775BB` | Blue highlights | Hover states, chart series 1 |
| `--sm-blue-deep` | `#294785` | Deep blue shadow | Pressed states, chart series 2 |
| `--sm-navy` | `#1B263C` | Dark blue inner shadow | Headings, table headers, sidebar |
| `--sm-silver` | `#D4D7D9` | Outer ring | Borders, dividers, disabled states |
| `--sm-surface` | `#F5F6F7` | (derived from silver) | Table stripes, panels |

Blue means "you can act here" (buttons, links, the selected tab). It never means a status.

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

- Black sidebar with the logo at the top and the tabs (No Movement, Inventory, Invoices, and later others) below it.
- White page, navy headings, silver borders.
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
