# CurriePing

[![Version](https://img.shields.io/github/v/release/MichaelCurrie/CurriePing)](https://github.com/MichaelCurrie/CurriePing/releases/latest)
[![CircleCI](https://img.shields.io/circleci/build/github/MichaelCurrie/CurriePing/main)](https://app.circleci.com/pipelines/github/MichaelCurrie/CurriePing)

A tiny self-hosted uptime monitor in the style of [Atlassian Statuspage](https://status.claude.com). It **pings the sites itself** on an interval and stores results in SQLite. Alerts to your phone/email via [https://ntfy.sh/](ntfy.sh). 

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
| **CurriePing** | **$0** | **unlimited** | 0.5 GB RAM → `t4g.nano` ~ **$3** |

## How to Deploy - via one-shot LLM prompt

Paste into an LLM agent (Claude Code, etc.):

> You are an agent that will help me deploy a status webpage. Follow these steps, prompting me as required.
> 
> 1. Confirm the `aws` CLI and `cloudflared` are installed and that API credentials are loaded for both. If either tool is missing or not authenticated, stop and tell me how to install/configure it.
> 
> 2. Tell me to install the ntfy.sh app on my phone
> 
> 3. Ask me to pick a long random topic string for status updates and subscribe to it in the app on my phone so the server can POST alerts there. (e.g. https://ntfy.sh/status-rforjgeorij234)
> 
> 4. Ask me for these .env values (examples provided):
> ``` 
> STATUS_DOMAIN=status.example.com
> STATUS_TITLE=My Service Status Webpage
> TARGETS=microsoft=https://www.microsoft.com,google=https://www.google.com
> NTFY_URL=https://ntfy.sh/status-rforjgeorij234
> ```
> 
> 5. Read the https://github.com/MichaelCurrie/CurriePing README follow all instructions

## How to Deploy - Manually

### How it runs

Everything runs in Docker Compose — two containers on a private network:

| Service | Role |
|---|---|
| `app` | Python CurriePing process (probes sites, SQLite history, status page on `:8080` inside the network) |
| `proxy` | [Caddy](https://caddyserver.com) — public `:80`/`:443`, TLS via Let's Encrypt, reverse-proxies to `app` |

For manual deploy instructions, see [INSTALL.md](INSTALL.md)
