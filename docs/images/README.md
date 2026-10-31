# README images

The social preview and banners use the **Planning Hub** design selected on
31 October 2026: a cyan glass planning calendar connects solar, batteries,
EV charging and climate to a ceramic solar home. Teal, emerald and warm yellow
accents connect the design to the Enphase Energy and Groq repository artwork.
This illustration is conceptual artwork, not a screenshot or measured forecast.
The integration icon is unchanged.

`social-preview.png` is the approved option 2 artwork exported at **1280 × 640**,
ready for upload under GitHub repository Settings → General → Social preview.
Committing the file does not update that repository setting automatically.

`readme-banner-{light,dark}.png` are **1200 × 400**. The README follows the reader's
color scheme and falls back to the light image. The text-free masters are in
`artwork/planning-hub-{light,dark}.png`; these were adapted from the approved
social image with the built-in image generator. Exact headings are rendered
from native SVG typography by `scripts/generate-doc-banners.cjs`, using Arial
with Helvetica/sans-serif fallbacks. Inspect regenerated images when using
different system fonts. The social preview retains its approved typography.

To regenerate banners, install Node.js and Sharp in a temporary directory, then
run from the integration checkout:

```bash
npm install --prefix /tmp/energy-planner-banner-tools --no-save --no-package-lock sharp@0.35.5
NODE_PATH=/tmp/energy-planner-banner-tools/node_modules node scripts/generate-doc-banners.cjs
```

To also regenerate and connect all 18 page-specific banners in a separate wiki
checkout, append `--wiki /path/to/ha-energy-planner.wiki`. This also copies the
social preview as `images/social-preview-planning-hub.png`. The wiki uses the
dark artwork for a consistent documentation masthead. Existing page headings,
anchors and guide content are retained. Updating the wiki requires its own
commit and push; it is a separate Git repository.

Screenshots were captured from a configured Energy Planner **1.4.9** installation
on **31 October 2026**, using the built-in browser in both Home Assistant themes.
They are real interface captures, cropped without changing controls, readings
or status text. These are examples, not setup defaults or evidence of savings.

| Image pair | Dimensions | Contents |
| --- | --- | --- |
| `planner-controls-{light,dark}.jpg` | 378 × 480 | Per-area controls and Explain |
| `planner-sensors-{light,dark}.jpg` | 378 × 610 | State, next actions and recovery |
| `planner-diagnostics-{light,dark}.jpg` | 378 × 369 | Forecast evidence and plan health |
| `plan-calendar-{light,dark}.jpg` | 757 × 850 | Confirmed history and planned windows |

The calendar captures show only the Energy Planner calendar. Browser chrome,
addresses, account details and unrelated devices are outside the final crops.
Automatic theme preference, calendar filters and the month view were restored
after capture. Device settings were unchanged. Live values and planned windows
can differ between theme captures as the planner refreshes.

When refreshing these images, capture both themes, retain natural proportions,
inspect the saved files for personal information and metadata, and update the
date and version here. The README uses `<picture>` to follow the reader's color
scheme and falls back to the light image. Keep footer badges outside `<details>`.
