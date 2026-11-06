# Integration branding

The approved calendar mark replaces the earlier circular lightning emblem as
of 6 November 2026. It has three binding tabs matching the border, plus three
schedule bars: solar yellow, battery green and EV blue. The bars retain the
tighter spacing approved during design review. The October banner-generation
notes in `docs/images/README.md` describe the earlier artwork refresh; this
document records the subsequent integration-icon replacement.

## Shipped assets

Home Assistant 2026.3 and later loads custom integration images directly from
`custom_components/ha_energy_planner/brand/`. These transparent PNGs follow the
[official brand image specifications](https://contributors.home-assistant.io/core/integrations/brands/):

| Files | Dimensions |
| --- | --- |
| `icon.png`, `dark_icon.png` | 256 × 256 |
| `icon@2x.png`, `dark_icon@2x.png` | 512 × 512 |
| `logo.png`, `dark_logo.png` | 548 × 256 |
| `logo@2x.png`, `dark_logo@2x.png` | 1096 × 512 |

The four corresponding SVGs are the editable sources. The artwork is trimmed
to its bounds; square icons retain only the tiny horizontal allowance needed
for the calendar's slightly taller aspect ratio. The logos retain the existing
two-line “HA Energy / Planner” wordmark with the new calendar mark. SVGs and
PNGs at the component root and `.github/assets/icon.*` mirror the standard
light assets for existing repository consumers.

## Palette

| Element | Hex | Source |
| --- | --- | --- |
| Solar | `#FFD029` | Dark banner pixel `(920, 124)` |
| Battery | `#1CD587` | Dark banner pixel `(890, 169)` |
| EV charging | `#01B1FD` | Dark banner pixel `(915, 221)` |
| Light border and tabs | `#003244` | Banner generator light foreground |
| Dark border and tabs | `#70E8ED` | Banner generator dark accent |

Pixel coordinates are zero-based from the top left of
`docs/images/readme-banner-dark.png`. The artwork uses gradients, so the flat
category fills use these exact representative samples. Wordmark fills use the
matching banner foreground/secondary colours: `#003244` / `#155366` in light
mode and `#F5FCFF` / `#C0E1E8` in dark mode.

## Regeneration

Render the native SVGs with `resvg-py==0.5.0`. The wordmarks use Arial; keep that
font installed when regenerating to preserve the approved typography and trim.
The renderer is an authoring dependency, not an integration dependency.

From the repository root:

```bash
uv run --no-project --with resvg-py==0.5.0 python - <<'PY'
from pathlib import Path
import shutil
import resvg_py

brand = Path('custom_components/ha_energy_planner/brand')
for prefix in ['', 'dark_']:
    for kind, width in [('icon', 256), ('logo', 548)]:
        source = brand / f'{prefix}{kind}.svg'
        for scale, suffix in [(1, ''), (2, '@2x')]:
            output = brand / f'{prefix}{kind}{suffix}.png'
            output.write_bytes(resvg_py.svg_to_bytes(
                svg_path=str(source), width=width * scale, height=256 * scale,
            ))
for name in ['icon.svg', 'icon.png', 'logo.svg', 'logo.png']:
    shutil.copyfile(brand / name, brand.parent / name)
for name in ['icon.svg', 'icon.png']:
    shutil.copyfile(brand / name, Path('.github/assets') / name)
PY
```

Verify PNG dimensions, transparency, visual appearance in both themes, and
that release archives contain all eight brand PNGs. Run
`scripts/docker-validate.sh` before considering the change complete.

The original exploration images and prompts are preserved in
`docs/images/artwork/icon-concepts/`, outside the installable integration.
