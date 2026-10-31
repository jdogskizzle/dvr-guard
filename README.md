# dvr-guard

Keeps Jellyfin's one-time Live TV recordings from disappearing.

Every time Jellyfin refreshes the guide, it deletes any one-time recording whose program it
can't find in the new guide, for example because the listing's start time moved by a minute.
It does this without a message, so the recording just never happens.

dvr-guard keeps its own list of every one-time recording you schedule in Jellyfin. When one
disappears during a guide refresh, it finds the same show in the new guide (same channel and
title, nearest start time) and schedules it again in Jellyfin with your original padding.
If the show is gone from the guide entirely, it records the saved time slot itself, straight
from the tuner, into your recordings folder.

Recordings you cancel in Jellyfin are left alone. Jellyfin only drops recordings while its
"Refresh Guide" task runs, and only when the program left the guide, so anything else that
disappears was a cancel.

Series recordings ("Record Series") don't need this: Jellyfin rebuilds them from the guide
on every refresh.

## Setup
1. In Jellyfin: Dashboard > API Keys > +, name it `dvr-guard`.
2. `cp .env.example .env` and fill it in.
3. `docker compose up -d`

Log: `docker logs -f dvr-guard`. Its list of recordings is in `data/recordings.json`.

## Settings (.env)
- `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `DOCKER_NETWORK`: how to reach Jellyfin.
- `SOURCE_M3U`: the tuner's M3U playlist, used only for recording straight from the tuner.
  Leave empty to skip that.
- `RECORD_DIR`: where those recordings go (usually Jellyfin's recording folder).
- `FALLBACK_EXTRA_MINUTES`: added to the end of tuner recordings, since sports run long.
- `PUID`, `PGID`, `TZ`.

Optional, in the environment: `POLL_SECONDS` (30), `MATCH_HOURS` (how far from the old start
to look for the show, 3), `DRY_RUN=1` (log what it would do without doing it).

## Limits
- Tuner recordings don't get Jellyfin's metadata until the library rescans the folder.
- If you cancel a recording at the same moment a guide refresh removes that program,
  dvr-guard can't tell the two apart and will put it back.
