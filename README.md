# dvr-guard

Records live TV for Jellyfin, reliably, and shares one tuner per channel between recordings
and viewers.

Jellyfin's own recordings have two problems:
- A recording shares the live-TV stream with anyone watching the same channel, and Jellyfin
  closes that stream when the viewers leave, even while it's recording. The recording just
  stops.
- Every guide refresh deletes one-time recordings whose program it can't find any more (for
  example because the start time moved a minute), without a message.

## How it works
- **Tuner hub.** dvr-guard serves the tuner's channels as an M3U playlist. Jellyfin (and
  anything else, like admute) uses it as its tuner. Each channel is pulled from the tuner
  once, however many viewers and recordings use it, and reconnected within seconds if the
  stream drops. If a recording needs a tuner and all are busy with viewers, a viewer-only
  channel is stopped to free one.
- **Recordings.** Press Record in Jellyfin as usual. Within a few seconds dvr-guard copies
  the recording (channel, times, padding, title, description) into its own schedule and
  removes it from Jellyfin's, so Jellyfin never records it itself. It records from the hub
  into Jellyfin's recording folder with Jellyfin's file names and `.nfo` files, so it shows up
  in the library as before. Viewers coming and going can't affect it. If the show moves in
  the guide, the recording moves with it. Gaps (tuner trouble) are logged and shown.
- **Games.** A recording whose listing names two teams ("Chicago Bears at Seattle
  Seahawks") is matched to the game on ESPN's public scoreboard. While the game is live it
  keeps recording past its slot (up to `GAME_MAX_HOURS`), and it stops `FINAL_MINUTES`
  after the game is final, padding or not, which frees the tuner early.
- **Too many at once.** When more channels need recording at the same time than there are
  tuners (padding included), the recordings are flagged on the status page as soon as
  they're scheduled. If it still happens, what's already recording carries on and the
  later one starts when a tuner frees up, with the missing part noted. One that never gets
  a tuner is marked missed and leaves no file behind.
- **Status page** (formlogin sign-in): what's recording, what each channel is serving,
  what's scheduled (with Cancel) and recent recordings with any missing seconds.

Series recordings work too: each episode Jellyfin schedules from a series is taken over.
Scheduled recordings therefore don't appear in Jellyfin's "Scheduled" list; see the status
page.

## Setup
1. In Jellyfin: Dashboard > API Keys > +, name it `dvr-guard`.
2. `cp .env.example .env` and fill it in.
3. `docker compose up -d --build`
4. In Jellyfin: Dashboard > Live TV > Tuner Devices: add an **M3U Tuner** with URL
   `<PUBLIC_URL>/lineup.m3u` (e.g. `http://dvr-guard:8000/lineup.m3u`), then remove the
   tuner's own entry (e.g. the HDHomeRun), so all live TV goes through the hub. The guide
   matches the channels by number as before.
5. Optionally put the status page behind your reverse proxy, e.g. Caddy
   `handle_path /dvr/* { reverse_proxy dvr-guard:8000 }`. The playlist and streams only
   answer requests that come straight from a private address, not through the proxy.

Log: `docker logs -f dvr-guard`. The schedule is in `data/schedule.json`. Restarting the
service during a recording leaves a gap of a few seconds; it carries on afterwards.

## Settings (.env)
- `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `DOCKER_NETWORK`: how to reach Jellyfin.
- `SOURCE_M3U`: the tuner's M3U playlist (HDHomeRun: `http://<ip>/lineup.m3u`).
- `PUBLIC_URL`: this service's address as Jellyfin sees it, used in the playlist.
- `RECORD_DIR`: the host folder Jellyfin records into (its Live TV recording path).
- `PUID`, `PGID`, `EXTRA_GID` (a group that can write to `RECORD_DIR`), `TZ`.
- `EXTRA_MINUTES`: added to every recording's end on top of Jellyfin's padding.
- `DVR_USERNAME`: optional, fixes the status page's login name. Set the password with
  `docker compose exec dvr-guard python -m formlogin passwd` (saved in `data/login.json`,
  used right away), or on first start on the setup page, with the code from `docker logs dvr-guard`.

- `TUNER_COUNT`: how many tuners the source has, for the overlap warning. Leave empty for
  an HDHomeRun (it's asked).

Optional, in the environment: `POLL_SECONDS` (how often Jellyfin is checked, 15),
`LINGER` (seconds a channel stays tuned after its last user, 10), `GAMES=0` (don't follow
games), `FINAL_MINUTES` (5), `GAME_MAX_HOURS` (2).

## Limits
- Jellyfin's series options like "keep up to N episodes" aren't applied.
- Two recordings at once on a two-tuner device leave no tuner for watching other channels.
