# Icon concepts

Archived design exploration. The approved v3 geometry is now installed in the
integration's `brand/` assets, with trimmed production exports documented in
`docs/branding.md`.

## Current spacing and dark-mode revision

`01-planning-calendar-v3-light.svg` and `01-planning-calendar-v3-dark.svg`
move all three schedule bars up by 11 units in the 256-unit view box, reducing
the gap above the yellow bar while keeping equal row spacing. The dark variant
uses the banner generator's dark-theme accent (`#70E8ED`) for both the border
and the three binding tabs. Solar, battery and EV fills retain the exact
sampled values below. Each SVG has a transparent 1024 px PNG export and a
512 px preview. `01-planning-calendar-v3-comparison.png` shows light mode on
the left and dark mode on the right. Preview backgrounds are `#F3FCFF` and
`#001B29` respectively; backgrounds are not part of the transparent icons.

This revision edits the native SVG directly and renders PNGs with resvg-py.
The integration icon was subsequently replaced with the approved v3 design.

## Selected calendar revision

`01-planning-calendar-v2.svg` is the exact-colour source, with a transparent
1024 px PNG export and a 512 px light-background preview. All three binding
tabs match the navy border (`#003244`). Horizontal bars represent solar
(`#FFD029`), battery (`#1CD587`) and EV charging (`#01B1FD`). These three flat
swatches are exact pixels from `docs/images/readme-banner-dark.png` at
`(920,124)`, `(890,169)` and `(915,221)` respectively, using zero-based
coordinates from the top left. The banner is gradient raster artwork rather
than a flat-colour specification.

The initial revised concept used built-in imagegen; the SVG and its PNG export
use explicit fills to guarantee the sampled hex values. PNGs are rendered with
resvg-py. The subsequent requested change sets all three binding tabs to the
border colour, preserving the three horizontal category colours.

### Initial revision prompt

Use case: precise-object-edit. Edit the provided Energy Planner calendar icon. Keep its deep navy #003244 rounded calendar frame, silhouette, bold flat minimal style, transparent background, spacing and the widths and positions of the THREE horizontal schedule bars. Make the TOP horizontal bar solar yellow #FFD029, MIDDLE horizontal bar battery green #1CD587, BOTTOM horizontal bar EV blue #01B1FD. Change from TWO to exactly THREE evenly spaced vertical rounded binding tabs along the top edge of the calendar, all the same size and height. LEFT binding tab solar yellow #FFD029, CENTER binding tab battery green #1CD587, RIGHT binding tab EV blue #01B1FD. Exactly three vertical tabs and three horizontal bars. These hex values were sampled directly from the supplied banner reference: solar at pixel (920,124), battery at (890,169), EV at (915,221). Flat solid colours with these exact RGB values, no gradients, glow, texture or decoration. Image 1 is the EDIT TARGET. Image 2 is a REFERENCE ONLY for the existing brand. Preserve transparency. No text or extra elements.

Three historical review candidates generated with the built-in imagegen tool.
PNGs have transparent backgrounds.
`comparison.png` shows the candidates against a light background for review.

Palette references: `docs/images/readme-banner-dark.png` and
`docs/images/readme-banner-light.png`.

## Shared generation prompt

Use case: logo-brand. Create ONE clean square app/integration icon concept for Energy Planner for Home Assistant. Input images are palette and brand-mood REFERENCES ONLY, not edit targets. Use the references' deep navy #003244, cyan #16D8ED, mint #42E4C0 and optional tiny solar gold #FFC83D; predominantly cyan and mint. Translate the banners into a radically simpler FLAT vector-like mark. Crisp solid strokes, consistent bold geometry, generous negative space, centered composition, excellent legibility at 32 and 48 px. Transparent background around the mark. No text, lettering, labels, watermark, mockup, lighting, glow, glass, shadows, photorealism, gradients, extraneous rings, decorative nodes or extra device pictograms. One icon only, no variants or presentation sheet.

## 01-planning-calendar.png

Subject: a minimal rounded calendar outline in deep navy with two short cyan binding tabs, enclosing exactly THREE thick horizontal schedule bars of differing lengths: cyan, mint, cyan. Calendar is a single simple symbol; no calendar grid, numbers, checkmark, sun, lightning bolt or extra objects. Occupy about 78 percent of square. Make the bars feel like the energy schedule in the reference banner while remaining very minimal.

## 02-home-energy.png

Subject: a single bold cyan house outline with a simple pitched roof and rounded lower corners, containing ONE solid mint lightning bolt. The bolt is chunky, geometrically balanced, generous space separating bolt and walls. No windows, doors, calendar, chimney, sun, circles or extra elements. Occupy about 78 percent of square. Recognisable home energy in two simple shapes.

## 03-energy-curve.png

Subject: a minimal cyan open circular arc, thick and smooth, surrounding ONE mint energy planning curve: a single smooth rising and falling hump, like a clean solar forecast line. A tiny warm gold dot caps the upper right end of the arc to recall the banners' solar accent. Only those three elements. Generous separation, confident clean geometry, no axes, arrowheads, lightning bolt, calendar, house, extra dots or extra lines. Occupy about 78 percent of square. Abstract, calm, distinctive energy planning mark.

## Cleanup prompt applied separately to each concept

Use case: precise-object-edit. Clean up this minimal app icon, preserving exactly its existing geometry, subject, composition, cyan/mint/navy/gold palette and genuine transparent background. Remove ALL scattered coloured pixels, distressed texture, ghosting, grain and debris from every negative-space region and outside the symbol. All empty spaces must be perfectly transparent. Make each main shape perfectly smooth, solid and flat like a professionally exported SVG. Remove gradients from the main shapes. Crisp antialiasing confined strictly to main shape boundaries. Preserve the main symbol; do not add or remove any primary component. No text, no watermark, no background rectangle.
