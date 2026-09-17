# arr-queue-doctor

Cleans up stuck TV and movie downloads so you don't have to.

If you run Sonarr and Radarr with qBittorrent, you know the problem: a download sits at 0% for days, or finishes but isn't actually a video file. Normally you'd have to find it, delete it, block it, and search for a replacement by hand. This does that for you.

## What it does

It has one simple job, every 15 minutes:

1. **Finds the stuck ones.** Downloads waiting too long with no progress, or finished files that aren't videos (or look unsafe, like an `.exe` hiding as a show).
2. **Throws out the bad one.** Removes it from Sonarr/Radarr and blocks it so the same bad file isn't grabbed again.
3. **Gets a better one.** Picks the healthiest replacement it can already see, or starts a fresh search if there's nothing good.

That's it. You keep watching like normal. You never clear a stuck queue by hand.

A small example:

1. An episode grabs a release with no one sharing it. It sits at 0% for over 90 minutes.
2. The doctor removes it, blocks that release, and grabs the same episode from a healthy source instead.
3. It writes one line to a log file so you can see what happened.

Movies work the same way. To stay safe it fixes at most 2 items per run, oldest first, so one bad night can't empty your whole queue.

## What you need

- Sonarr (TV shows) and/or Radarr (movies)
- qBittorrent doing the downloading
- All three on the same machine
- In qBittorrent, turn on "Bypass authentication for clients on localhost" (Settings > WebUI). The doctor talks to qBittorrent without a login, so it needs this.
- Python 3.10 or newer (no extra packages needed)

## Install

```bash
sudo cp arr-queue-doctor.py /usr/local/lib/arr-queue-doctor.py
sudo cp arr-queue-doctor.service arr-queue-doctor.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now arr-queue-doctor.timer
```

The doctor needs your Sonarr/Radarr API keys. Easiest is to set them directly:

```bash
sudo systemctl edit arr-queue-doctor.service
```

```ini
[Service]
Environment=SONARR_API_KEY=paste-your-key-here
Environment=RADARR_API_KEY=paste-your-key-here
```

Find each key in Sonarr/Radarr under Settings > General > Security > API Key. If you'd rather point at their config files instead, see `.env.example`.

## Is it working?

```bash
systemctl status arr-queue-doctor.timer
journalctl -u arr-queue-doctor.service -n 50
cat /var/lib/arr-queue-doctor/recovery-events.jsonl
```

The last command shows one line per fix, in plain JSON. See `recovery-events.jsonl.example` for what a line looks like.

Want to try it safely first? This logs what it *would* do without deleting or downloading anything:

```bash
sudo ARR_QUEUE_DOCTOR_DRY_RUN=1 python3 /usr/local/lib/arr-queue-doctor.py
```

## Settings

Most people never change these. Copy `.env.example` if you need to.

| Setting | What it means | Default |
|---|---|---|
| `SONARR_URL` / `RADARR_URL` / `QBIT_URL` | Where Sonarr, Radarr, and qBittorrent live | Same machine (`127.0.0.1`) |
| `SONARR_API_KEY` / `RADARR_API_KEY` | Passwords the doctor uses to talk to Sonarr/Radarr | Empty (reads their config files instead) |
| `SONARR_CONFIG` / `RADARR_CONFIG` | Where to find the API key if you don't set it above | `/var/lib/sonarr/config.xml` and `/var/lib/radarr/config.xml` |
| `ARR_QUEUE_DOCTOR_META_TIMEOUT` | Give up waiting for download info after this (seconds) | `1800` (30 min) |
| `ARR_QUEUE_DOCTOR_STALL_TIMEOUT` | Give up on a zero-progress download after this (seconds) | `5400` (90 min) |
| `ARR_QUEUE_DOCTOR_HTTP_TIMEOUT` | Give up on a single request to Sonarr/Radarr/qBittorrent after this (seconds) | `90` |
| `ARR_QUEUE_DOCTOR_MAX_RECOVERIES` | Most items fixed per run | `2` |
| `ARR_QUEUE_DOCTOR_SKIP_TAGS` | Leave stalled downloads with these qBittorrent labels alone | `route_error,route_import_failed,route_overcommit,route_waiting_space` (empty = fix everything stuck; unsafe or non-video payloads are always cleaned regardless of labels) |
| `ARR_QUEUE_DOCTOR_DRY_RUN` | `1` = log only, change nothing | `0` |
| `ARR_QUEUE_DOCTOR_LOG` | Where the fix log lives | `/var/lib/arr-queue-doctor/recovery-events.jsonl` |

## What it never does

- Never deletes anything from your finished library. It only touches stuck queue items.
- Never shares your keys. They stay on your machine, never in this repo.
- Never opens anything to your network. It talks to Sonarr/Radarr/qBittorrent on the same machine only.
- One failure doesn't stop the rest. If one fix errors, the others still run and the error is logged.

## Files

```text
arr-queue-doctor.py            # the doctor, Python stdlib only
arr-queue-doctor.service       # runs it
arr-queue-doctor.timer         # every 15 minutes
.env.example                   # all settings with examples
recovery-events.jsonl.example  # what the log looks like (fake data)
```

License: MIT. See `LICENSE`.
