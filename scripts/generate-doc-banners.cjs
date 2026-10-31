#!/usr/bin/env node
/* Render native SVG typography over the approved planning-hub artwork.
 * Dependency: sharp 0.35.5. See docs/images/README.md for regeneration.
 * Run with --wiki /path/to/wiki to also refresh the separate wiki checkout.
 */
"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");
const sharp = require("sharp");

const ROOT = path.resolve(__dirname, "..");
const BANNERS = [
  ["Home", "home", ["Energy", "Planner"], ["Smart energy planning for Home Assistant"]],
  ["Installation", "installation", ["Installation"], ["Install, update and connect to Home Assistant."]],
  ["Hub-and-Input-Setup", "hub-and-input-setup", ["Setup and", "Configuration"], ["Your inputs, devices and planning preferences."]],
  ["Energy-Planning", "energy-planning", ["Energy Planning"], ["Tariffs, solar, load forecasts and battery reserves."]],
  ["EV-Charging", "ev-charging", ["EV Charging"], ["The right charge, at the right time."]],
  ["Climate-and-Presence", "climate-and-presence", ["Climate and", "Presence"], ["Comfort-aware planning for your home."]],
  ["Enphase-and-AI", "enphase-and-ai", ["Enphase and AI"], ["Battery profiles and on-demand explanations."]],
  ["Safety-and-Production", "safety-and-production", ["Safety and", "Production"], ["Review, arm, pause and recover safely."]],
  ["Troubleshooting", "troubleshooting", ["Troubleshooting"], ["Understand the evidence. Find the next step."]],
  ["Development", "development", ["Development"], ["Architecture, validation and contribution guides."]],
  ["Upgrading-and-Changes", "upgrading-and-changes", ["Upgrading", "and Changes"], ["Release changes and migration guidance."]],
  ["Action-Allowances", "action-allowances", ["Action Allowances"], ["Rolling limits and resuming climate planning."]],
  ["EV-Load-Recovery", "ev-load-recovery", ["EV Load Recovery"], ["Charging through bounded consumption outages."]],
  ["EV-Vehicles", "ev-vehicles", ["EV Vehicles"], ["Multiple cars sharing one charger."]],
  ["Enphase-Export-Limit", "enphase-export-limit", ["Enphase", "Export Limit"], ["Independent, tariff-aware export control."]],
  ["Known-Limitations", "known-limitations", ["Known Limitations"], ["Requirements, boundaries and evidence limits."]],
  ["Plan-Calendar", "plan-calendar", ["Plan Calendar"], ["Upcoming plans and confirmed activity."]],
  ["Recovery-Diagnostics", "recovery-diagnostics", ["Recovery", "Diagnostics"], ["Understand blockers and restore control safely."]],
];

function escape(value) {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

async function render(background, theme, lines, subtitle, home, output) {
  const light = theme === "light";
  const foreground = light ? "#003244" : "#f5fcff";
  const secondary = light ? "#155366" : "#c0e1e8";
  const accent = light ? "#006d7a" : "#70e8ed";
  const fontSize = home ? 86 : 52;
  const firstBaseline = home ? 174 : lines.length === 1 ? 196 : 165;
  const lineHeight = home ? 86 : 62;
  const subtitleBaseline = home ? 308 : lines.length === 1 ? 249 : 274;
  const title = lines.map((line, index) =>
    `<text x="48" y="${firstBaseline + index * lineHeight}" font-size="${fontSize}" font-weight="700" letter-spacing="-1.6">${escape(line)}</text>`
  ).join("");
  // Text is measured with the same installed font before rendering; nothing may
  // cross into the right-hand illustration or be squeezed to fit a long title.
  for (const line of lines) {
    const measure = await sharp({text: {text: escape(line), font: `Arial Bold ${fontSize}`, rgba: true}}).metadata();
    if (measure.width > 555) throw new Error(`Title exceeds text area: ${line}`);
  }
  for (const line of subtitle) {
    const measure = await sharp({text: {text: escape(line), font: "Arial 21", rgba: true}}).metadata();
    if (measure.width > 555) throw new Error(`Subtitle exceeds text area: ${line}`);
  }
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="400" viewBox="0 0 1200 400">
    <image width="1200" height="400" href="data:image/png;base64,${background.toString("base64")}"/>
    <g font-family="Arial, Helvetica, sans-serif" fill="${foreground}">
      <text x="50" y="${home ? 67 : 82}" fill="${accent}" font-size="13" font-weight="700" letter-spacing="2.3">${home ? "HOME ASSISTANT INTEGRATION" : "ENERGY PLANNER / GUIDE"}</text>
      ${title}
      ${subtitle.map((line, index) => `<text x="50" y="${subtitleBaseline + index * 27}" fill="${secondary}" font-size="21">${escape(line)}</text>`).join("")}
      <text x="50" y="350" fill="${secondary}" font-size="15">${home ? "Solar · Batteries · EV charging · Climate" : "Energy Planner · Home Assistant"}</text>
    </g>
  </svg>`;
  await sharp(Buffer.from(svg)).png().toFile(output);
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length && (args.length !== 2 || args[0] !== "--wiki")) {
    throw new Error("Usage: node scripts/generate-doc-banners.cjs [--wiki /path/to/wiki]");
  }
  const integration = await fs.access(path.join(ROOT, "docs/images/artwork")).then(() => true, () => false);
  const wiki = args.length ? path.resolve(args[1]) : integration ? null : ROOT;
  const imageRoot = path.join(ROOT, integration ? "docs/images" : "images");
  if (integration) {
    for (const theme of ["dark", "light"]) {
      const background = await fs.readFile(path.join(imageRoot, `artwork/planning-hub-${theme}.png`));
      await render(background, theme, ["Energy", "Planner"], ["Smart energy planning for Home Assistant"], true, path.join(imageRoot, `readme-banner-${theme}.png`));
    }
  }
  if (wiki) {
    const background = await fs.readFile(path.join(imageRoot, "artwork/planning-hub-dark.png"));
    await fs.mkdir(path.join(wiki, "images/banners"), {recursive: true});
    for (const [page, slug, lines, subtitle] of BANNERS) {
      const filename = path.join(wiki, `${page}.md`);
      let markdown = await fs.readFile(filename, "utf8");
      await render(background, "dark", lines, subtitle, page === "Home", path.join(wiki, `images/banners/${slug}.png`));
      const title = page === "Home" ? "Energy Planner" : lines.join(" ");
      const banner = `<img src="images/banners/${slug}.png" alt="${escape(title)} — ${escape(subtitle.join(" "))}" width="1200">`;
      if (/^<img src="images\/banners\/[^\n]+/.test(markdown)) {
        markdown = markdown.replace(/^<img src="images\/banners\/[^\n]+/, banner);
      } else {
        // Retain existing text headings and anchor links on the newer pages.
        markdown = `${banner}\n\n${markdown}`;
      }
      await fs.writeFile(filename, markdown);
    }
    if (integration) {
      await fs.mkdir(path.join(wiki, "images/artwork"), {recursive: true});
      await fs.mkdir(path.join(wiki, "scripts"), {recursive: true});
      await fs.copyFile(path.join(imageRoot, "artwork/planning-hub-dark.png"), path.join(wiki, "images/artwork/planning-hub-dark.png"));
      await fs.copyFile(__filename, path.join(wiki, "scripts/generate-doc-banners.cjs"));
      await fs.copyFile(path.join(imageRoot, "social-preview.png"), path.join(wiki, "images/social-preview-planning-hub.png"));
    }
  }
  console.log(`Rendered ${integration ? "2 README banners" : ""}${integration && wiki ? " and " : ""}${wiki ? `${BANNERS.length} wiki banners` : ""}.`);
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
