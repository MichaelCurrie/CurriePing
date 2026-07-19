# CurriePing

[![Version](https://img.shields.io/github/v/release/MichaelCurrie/CurriePing)](https://github.com/MichaelCurrie/CurriePing/releases/latest)
[![CircleCI](https://img.shields.io/circleci/build/github/MichaelCurrie/CurriePing/main)](https://app.circleci.com/pipelines/github/MichaelCurrie/CurriePing)
[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-blue.svg)](LICENSE)

A tiny self-hosted uptime monitor in the style of [Atlassian Statuspage](https://status.claude.com). It **pings your sites** on an interval and stores results in SQLite. It sends alerts to your phone or email via [https://ntfy.sh/](ntfy.sh).

## Example

https://status.michaelcurrie.com

<img width="633" height="326" alt="image" src="https://github.com/user-attachments/assets/19847f23-c202-462a-adf5-fac50104476f" />

## Comparison

| Tool | Subscription cost / month | Max sites monitored | Hosting cost / month |
|---|---|---|---|
| [Better Stack](https://betterstack.com/pricing) | **$29** | 50 | — |
| [StatusCake](https://www.statuscake.com/pricing/) | **~$20** | 100 | — |
| [Pingdom](https://www.pingdom.com/pricing/) | **$15** | 10 | — |
| [UptimeRobot](https://uptimerobot.com/pricing/) | **$9** | 50 | — |
| [Uptime Kuma](https://github.com/louislam/uptime-kuma) | **$0** | unlimited | ~1 GB RAM → `t4g.micro` ~ **$6*** |
| **CurriePing** | **$0** | **unlimited** | 0.5 GB RAM → `t4g.nano` ~ **$3*** |

* Default deploy is IPv6-only (no AWS public IPv4, ~$3.65/mo saved). Production uses [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) so IPv4 browsers still reach the status page. Set `CHECK_IPV4=True` + an Elastic IP only if you also want the monitor to probe IPv4.

## How to Deploy - via one-shot LLM prompt

Paste into an LLM agent (Claude Code, etc.):

> You are an agent that will help me deploy a status webpage. Follow these steps, prompting me as required.
> 
> 1. Tell me to install the [ntfy.sh](https://ntfy.sh) app on my phone
> 
> 2. Ask me to pick a long random topic string for status updates and subscribe to it in the app on my phone so the server can POST alerts there. (e.g. https://ntfy.sh/status-rforjgeorij234)
> 
> 3. Ask me for these .env values (examples provided):
> ``` 
> STATUS_DOMAIN=status.example.com
> STATUS_TITLE=My Service Status Webpage
> TARGETS=microsoft=https://www.microsoft.com,google=https://www.google.com
> NTFY_URL=https://ntfy.sh/status-rforjgeorij234
> CHECK_IPV4=False
> ```
> Explain `CHECK_IPV4` (must be exactly `True` or `False`): False = cheapest IPv6-only EC2 (IPv6 probes only); True = attach an Elastic IP (~$3.65/mo) so the monitor also probes IPv4. IPv6 is always checked. The status page shows IPv4/IPv6 checkboxes for which families are enabled.
> 
> 4. Confirm the `aws` CLI is installed and authenticated. If not, stop and help me install it.
> 
> 5. Confirm they have a Cloudflare account. Help them create a Cloudflare Tunnel (Zero Trust → Networks → Tunnels) with public hostname `STATUS_DOMAIN` → `http://app:8080`, and copy the tunnel token into `CLOUDFLARE_TUNNEL_TOKEN`. DNS should be a CNAME to `<tunnel-id>.cfargotunnel.com` (or Cloudflare-managed if the zone is on Cloudflare) — not an A/AAAA to the EC2 address.
> 
> 6. Follow [INSTALL.md](https://github.com/MichaelCurrie/CurriePing/blob/main/INSTALL.md) to deploy (honor their CHECK_IPV4 choice in the launch script).

## How to Deploy - Manually

### How it runs

Docker Compose services:

| Service | Role |
|---|---|
| `app` | Python CurriePing process (probes sites, SQLite history, status page on `:8080` inside the network) |
| `proxy` | Optional [Caddy](https://caddyserver.com) on host `:80`/`:443` (local/dev or direct IPv6) |
| `tunnel` | Optional `cloudflared` (profile `tunnel`) — Cloudflare Tunnel to `app:8080` for public IPv4+IPv6 without a public AWS IPv4 |

See [INSTALL.md](INSTALL.md).

## License

[Unlicense](LICENSE) — public domain. Do anything with it.
