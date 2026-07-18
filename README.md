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
| [Uptime Kuma](https://github.com/louislam/uptime-kuma) | **$0** | unlimited | ~1 GB RAM → `t4g.micro` ~ **$6** |
| **CurriePing** | **$0** | **unlimited** | 0.5 GB RAM → `t4g.nano` ~ **$3** (IPv6-only; skip the ~$3.65 public IPv4 fee) |

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
> ```
> 
> 4. Confirm the `aws` CLI is installed and authenticated. If not, stop and help me install it.
> 
> 5. Ask where DNS for `STATUS_DOMAIN` is hosted (OpenSRS, Route53, Cloudflare DNS, etc.). The deploy is **IPv6-only**: they will create an **AAAA** record (and should remove any **A** record). Confirm their network can use IPv6 for SSH and for viewing the status page.
> 
> 6. Follow [INSTALL.md](https://github.com/MichaelCurrie/CurriePing/blob/main/INSTALL.md) to deploy.

## How to Deploy - Manually

### How it runs

Everything runs in Docker Compose — two containers on a private network:

| Service | Role |
|---|---|
| `app` | Python CurriePing process (probes sites, SQLite history, status page on `:8080` inside the network) |
| `proxy` | [Caddy](https://caddyserver.com) — public `:80`/`:443`, TLS via Let's Encrypt, reverse-proxies to `app` |

Production AWS install is **IPv6-only** (AAAA DNS, no Elastic/public IPv4) so the address fee does not dwarf the `t4g.nano`. See [INSTALL.md](INSTALL.md).

## License

[Unlicense](LICENSE) — public domain. Do anything with it.
