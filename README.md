# SUPIT

The buyer’s side of GPU compute. An investor-oriented website and interactive product preview for sourcing capacity, negotiating contracts, managing compute economics, and planning continuity.

**Live website:** [supit-compute.starrypine67.chatgpt.site](https://supit-compute.starrypine67.chatgpt.site)

## Run locally

No dependencies, GPU accounts, or API keys are required. From this repository:

```sh
python3 -m http.server 8001 --bind 127.0.0.1 --directory web
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001). On macOS, you can also double-click `start.command` and open the address it prints.

## Build

With Node.js installed:

```sh
node build.mjs
```

This checks JavaScript syntax and copies the website to `dist/`. Serve or deploy that directory with a static website host. The hosted website is managed separately through Sites; pushing to this GitHub repository does not automatically deploy it.

## Explore

| Page | Path |
| --- | --- |
| SUPIT vision and value proposition | `/` |
| Product overview | `/platform.html#overview` |
| Compute agreements | `/platform.html#deals` |
| Financial model and optional management-fee model | `/platform.html#finance` |
| Proposed protection terms and credit calculator | `/platform.html#credits` |
| Backup and recovery scenarios | `/platform.html#continuity` |
| Example terms and model exports | `/platform.html#documents` |
| Preserved browser-only compute-proof rehearsal | `/proof.html` |

The financial model supports editable compute assumptions, proposed credits, retained-payment timing, backup choices, and an optional SUPIT management fee. JSON and CSV exports are generated locally in the browser.

## Scope

This repository contains the website source only. It does not include the separate Python control plane, GPU agent, or gateway prototype.

All company names, suppliers, incidents, and financial terms shown in the workspace are illustrative. Inputs reset on reload. No money is held or moved, claims are submitted, suppliers are connected, or GPU capacity is reserved. The management fee is a model assumption rather than an actual pricing offer. Migration, storage, networking, and reservation costs are excluded from the modeled total.

Private signing keys, runtime evidence, credentials, and hosting-account configuration are not included.
