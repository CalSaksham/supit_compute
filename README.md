# SUPIT

The buyer’s side of GPU compute, starting with inference. SUPIT helps AI teams pick a provider on evidence, verify the GPUs at handover, keep traffic online through failures, and get credits back from a 10% holdback the buyer keeps.

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

This checks JavaScript syntax and the provider data, then copies the website to `dist/`.

After editing `web/contract.yaml`, regenerate the contract and its fingerprint (requires PyYAML):

```sh
python3 tools/hash_contract.py
```

This writes `web/contract.json` with the SHA-256 of the canonical contract JSON, matching what the demo verifies in the browser. Serve or deploy that directory with a static website host. The hosted website is managed separately through Sites; pushing to this GitHub repository does not automatically deploy it.

## Explore

| Page | Path |
| --- | --- |
| Homepage: problem, how it works, contract terms | `/` |
| Find a provider: ClusterMAX tier, SUPIT score, combined score, filters | `/providers.html` |
| Interactive demo: deal, handover, incidents, claim pack, evidence log | `/demo.html` |

`/proof.html` redirects to `/demo.html`, and `/platform.html` redirects to the homepage.

Provider data lives in `web/providers.json`. ClusterMAX tiers come from SemiAnalysis ClusterMAX 3.0; SUPIT scores are sample data until real deals are measured. Commercial fields (price, deposit, terms) are placeholders.

The demo follows Zorbe (an a16z speedrun company) renting 8 H100s from Lambda, with CoreWeave and Together AI as backups. Once handover passes, the demo deal is merged into Lambda's SUPIT and combined scores on Find a provider (via `localStorage`, live across tabs). Scoring lives in `web/scoring.js`, shared by both pages.

The demo contract is `web/contract.yaml`: an inference deal with a floor of 990 output tokens/s per GPU at p95 ≤ 500 ms, measured by the gateway. Downtime is credited at full price and slow time at half price, both across every GPU serving the model and capped by the holdback.

## Live mode (macOS, Apple silicon)

The demo has a **Rehearsal / Live · this Mac** toggle in its top bar. Rehearsal is the browser simulation above. Live runs real inference on your Mac:

```sh
./start-live.command
```

The first run creates `live/.venv`, installs the Python dependencies and pulls `gemma3:4b` through [Ollama](https://ollama.com/download) (install it first). It then starts three `ollama serve` processes that play Lambda (port 11501), CoreWeave (11502) and Together AI serverless (11503), and a control plane on [127.0.0.1:8787](http://127.0.0.1:8787/demo.html). Flip the toggle on the regular site, or open that address directly. Control-C stops everything.

What is real in live mode:

- **Traffic.** A load generator sends prompts through the gateway, which picks a backend, retries in-flight requests on failure and times every token. Call it yourself at `POST /v1/chat/completions` (OpenAI format); responses include a `supit` block showing which backend served them.
- **Faults.** "Take Lambda offline" SIGKILLs Lambda's Ollama process group. "Slow Lambda down" duty-cycles its model runner (SIGSTOP/SIGCONT, 15% on), like a power cap; health checks still pass. The gateway is never told about either; it detects outages from failed requests and health checks, and slowness from measured output speed, including requests still in flight.
- **Handover.** Seven checks measure Lambda against `live/contract.live.yaml` and the bar Lambda sets on first start (`live/data/bar.json`). NVIDIA DCGM diagnostics are skipped and recorded as skipped.
- **Bake-off, claim and evidence.** The bake-off measures Lambda and CoreWeave with the same prompts. Claims use measured downtime, slow time and rescue traffic. Every event is signed with a persistent Ed25519 key in `live/data/` and appended to a hash-chained log.

The 8 GPU tiles stay simulated (labelled as such), because a Mac has no NVIDIA GPUs. Provider names label local servers; nothing is sent to Lambda, CoreWeave or Together AI.

## Scope

This repository contains the website and the Mac live-mode backend in `live/`. It does not include a production control plane, GPU agent or gateway for real clusters.

Demo telemetry, incidents, and financial terms are illustrative. No money is held or moved, claims are submitted, providers are connected, or GPU capacity is reserved. SUPIT is not affiliated with SemiAnalysis.

Private signing keys, runtime evidence, credentials, and hosting-account configuration are not included.
