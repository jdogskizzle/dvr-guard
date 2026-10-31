"""Keeps Jellyfin's one-time Live TV recordings from being lost on a guide refresh.

When Jellyfin refreshes the guide, it deletes any one-time recording whose program it can't
find in the new guide, for example because the start time moved a minute. It does this
silently. This service remembers every one-time recording, and when one disappears during a
guide refresh (not a cancel), it schedules the same show again from the new guide. If the
show is no longer in the guide at all, it records the saved time slot straight from the
tuner.

A cancel in the Jellyfin UI is told apart from a refresh drop because Jellyfin only drops
recordings while its "Refresh Guide" task runs, and only when the program left the guide.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


def need(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is not set (see .env.example)")
    return value


JELLYFIN = need("JELLYFIN_URL").rstrip("/")
API_KEY = need("JELLYFIN_API_KEY")
SOURCE_M3U = os.environ.get("SOURCE_M3U", "").strip()
RECORD_DIR = os.environ.get("RECORD_DIR", "/recordings")
STATE = os.path.join(os.environ.get("DATA", "/data"), "recordings.json")
POLL = float(os.environ.get("POLL_SECONDS", "30"))
MATCH = timedelta(hours=float(os.environ.get("MATCH_HOURS", "3")))
EXTRA = timedelta(minutes=float(os.environ.get("FALLBACK_EXTRA_MINUTES", "30")))
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def now():
    return datetime.now(timezone.utc)


def parse_time(s):
    """Jellyfin dates look like 2026-10-31T00:00:00.0000000Z."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    s = re.sub(r"\.(\d{6})\d*", r".\1", s)
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f0Z")


def local(t):
    return t.astimezone().strftime("%a %b %d %H:%M")


# --- Jellyfin API -------------------------------------------------------------

def api(method, path, params=None, body=None):
    url = JELLYFIN + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f'MediaBrowser Token="{API_KEY}"',
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def get_timers():
    return api("GET", "/LiveTv/Timers").get("Items", [])


def guide_refresh():
    """(running, last finished) for Jellyfin's Refresh Guide task."""
    for task in api("GET", "/ScheduledTasks"):
        if task.get("Key") == "RefreshGuide":
            last = task.get("LastExecutionResult") or {}
            return task.get("State") == "Running", parse_time(last.get("EndTimeUtc"))
    raise RuntimeError("Jellyfin has no Refresh Guide task")


def channel_number(channel_id):
    ch = api("GET", f"/LiveTv/Channels/{channel_id}")
    return ch.get("ChannelNumber") or ch.get("Number")


def in_guide(rec):
    """Whether the recording's original program is still in the guide. Jellyfin only drops
    a recording when it isn't, so if it is, the recording was cancelled."""
    if not rec.get("program_id"):
        return False
    try:
        api("GET", f"/LiveTv/Programs/{rec['program_id']}")
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def find_program(rec):
    """The same show on the same channel in the current guide, nearest to the old start."""
    start = parse_time(rec["start"])
    items = api("GET", "/LiveTv/Programs", {
        "channelIds": rec["channel_id"],
        "minStartDate": iso(start - MATCH),
        "maxStartDate": iso(start + MATCH),
        "limit": 200,
    }).get("Items", [])
    same = [p for p in items if p.get("Name") == rec["name"]]
    if rec.get("episode"):
        same = [p for p in same if p.get("EpisodeTitle") == rec["episode"]] or same
    same.sort(key=lambda p: abs(parse_time(p["StartDate"]) - start))
    return same[0] if same else None


def schedule(rec, program):
    """Schedule `program` the way the Jellyfin UI does, with the original padding."""
    body = api("GET", "/LiveTv/Timers/Defaults", {"programId": program["Id"]})
    for key in ("PrePaddingSeconds", "PostPaddingSeconds", "IsPrePaddingRequired", "IsPostPaddingRequired"):
        if key in rec:
            body[key] = rec[key]
    api("POST", "/LiveTv/Timers", body=body)


# --- fallback recording -------------------------------------------------------

def stream_url(number):
    if SOURCE_M3U.startswith(("http://", "https://")):
        with urllib.request.urlopen(SOURCE_M3U, timeout=15) as resp:
            text = resp.read().decode("utf-8", "replace")
    else:
        with open(SOURCE_M3U, encoding="utf-8") as f:
            text = f.read()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
        nums = {attrs.get(k) for k in ("tvg-chno", "channel-number", "channel-id", "tvg-id")}
        if str(number) in nums:
            for nxt in lines[i + 1:]:
                if nxt.strip() and not nxt.startswith("#"):
                    return nxt.strip()
    return None


def safe(name):
    return re.sub(r'[\\/:*?"<>|]+', " ", name).strip()


def record(rec, store):
    """Record the saved time slot straight from the tuner, reconnecting until it ends."""
    start, end = parse_time(rec["start"]), parse_time(rec["end"])
    begin = start - timedelta(seconds=rec.get("PrePaddingSeconds", 0))
    stop = end + timedelta(seconds=rec.get("PostPaddingSeconds", 0)) + EXTRA
    title = rec["name"] + (f" - {rec['episode']}" if rec.get("episode") else "")
    path = os.path.join(RECORD_DIR, safe(f"{title} {start.astimezone():%Y-%m-%d %H.%M}") + ".ts")
    log(f"recording {title} ({rec['channel_name']}) from the tuner until {local(stop)} -> {path}")
    wait = (begin - now()).total_seconds()
    if wait > 0:
        time.sleep(wait)
    got = 0
    while now() < stop:
        try:
            url = stream_url(rec["channel_number"])
            if not url:
                log(f"channel {rec['channel_number']} is not in SOURCE_M3U, can't record {title}")
                break
            with urllib.request.urlopen(url, timeout=30) as resp, open(path, "ab") as out:
                while now() < stop:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
        except Exception as exc:  # tuner busy, network blip: keep trying until the slot ends
            log(f"recording {title}: {exc}, retrying")
        if now() < stop:
            time.sleep(10)
    log(f"finished recording {title}: {got / 1e9:.2f} GB")
    store.update(rec["id"], status="recorded")


# --- state --------------------------------------------------------------------

class Store:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        self.recs, self.checked = {}, None
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            self.recs, self.checked = d.get("recordings", {}), parse_time(d.get("checked"))
        except FileNotFoundError:
            pass

    def update(self, rid, **fields):
        with self.lock:
            if rid in self.recs:
                self.recs[rid].update(fields)
            self.save()

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"checked": self.checked and iso(self.checked), "recordings": self.recs}, f, indent=1)
        os.replace(tmp, self.path)


def from_timer(t, old=None):
    rec = dict(old or {})
    rec.update({
        "id": t["Id"],
        "status": "scheduled",
        "name": t.get("Name") or "",
        "episode": (t.get("ProgramInfo") or {}).get("EpisodeTitle") or "",
        "program_id": t.get("ProgramId"),
        "channel_id": t.get("ChannelId"),
        "channel_name": t.get("ChannelName") or "",
        "start": t["StartDate"],
        "end": t["EndDate"],
    })
    for key in ("PrePaddingSeconds", "PostPaddingSeconds", "IsPrePaddingRequired", "IsPostPaddingRequired"):
        if key in t:
            rec[key] = t[key]
    if not rec.get("channel_number"):
        try:
            rec["channel_number"] = channel_number(rec["channel_id"])
        except Exception as exc:
            log(f"couldn't look up the channel number for {rec['name']}: {exc}")
    return rec


def check(store, threads):
    checked_at = now()
    timers = get_timers()
    running, refreshed = guide_refresh()
    current = {t["Id"]: t for t in timers
               if not t.get("SeriesTimerId")  # series recordings are rebuilt by Jellyfin itself
               and t.get("Status") not in ("Cancelled", "Completed")}

    with store.lock:
        recs = store.recs
        for tid, t in current.items():
            if tid not in recs:
                recs[tid] = from_timer(t)
                log(f"watching {recs[tid]['name']} on {recs[tid]['channel_name']}, {local(parse_time(t['StartDate']))}")
            elif recs[tid]["status"] == "scheduled":
                recs[tid] = from_timer(t, recs[tid])

        if running:  # wait until the refresh finishes before deciding what was dropped
            store.save()
            return

        for rid, rec in list(recs.items()):
            end = parse_time(rec["end"]) + timedelta(seconds=rec.get("PostPaddingSeconds", 0))
            if rec["status"] == "scheduled" and rid not in current:
                if now() > end:
                    del recs[rid]  # finished
                elif refreshed and store.checked and refreshed > store.checked and not in_guide(rec):
                    rec["status"] = "lost"
                    log(f"Jellyfin dropped {rec['name']} ({local(parse_time(rec['start']))}) in a guide refresh")
                else:
                    log(f"{rec['name']} ({local(parse_time(rec['start']))}) was cancelled in Jellyfin, forgetting it")
                    del recs[rid]
            elif rec["status"] == "recorded" or (rec["status"] in ("restored", "lost") and now() > end + EXTRA):
                del recs[rid]

        for rid, rec in list(recs.items()):
            if rec["status"] != "lost":
                continue
            program = find_program(rec)
            if program:
                existing = next((t for t in current.values() if t.get("ProgramId") == program["Id"]), None)
                when = local(parse_time(program["StartDate"]))
                if existing:
                    log(f"{rec['name']} is already scheduled again ({when})")
                elif DRY_RUN:
                    log(f"[dry run] would schedule {rec['name']} again, now at {when}")
                else:
                    schedule(rec, program)
                    log(f"scheduled {rec['name']} again, now at {when}")
                rec["status"] = "restored"
            elif now() >= parse_time(rec["start"]) - timedelta(seconds=rec.get("PrePaddingSeconds", 0) + 120):
                if not SOURCE_M3U:
                    log(f"{rec['name']} is gone from the guide and SOURCE_M3U is not set, so it can't be recorded")
                    rec["status"] = "recorded"
                elif DRY_RUN:
                    log(f"[dry run] would record {rec['name']} from the tuner")
                    rec["status"] = "recorded"
                elif rid not in threads or not threads[rid].is_alive():
                    rec["status"] = "recording"
                    threads[rid] = threading.Thread(target=record, args=(rec, store), daemon=True)
                    threads[rid].start()

        # A recording that was in progress when the service restarted carries on.
        for rid, rec in recs.items():
            if rec["status"] == "recording" and (rid not in threads or not threads[rid].is_alive()):
                threads[rid] = threading.Thread(target=record, args=(rec, store), daemon=True)
                threads[rid].start()

        store.checked = checked_at
        store.save()


def main():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    store, threads = Store(STATE), {}
    log(f"watching Jellyfin at {JELLYFIN}" + (" (dry run)" if DRY_RUN else "")
        + ("" if SOURCE_M3U else "; SOURCE_M3U not set, so shows gone from the guide can't be recorded"))
    failing = False
    while True:
        try:
            check(store, threads)
            if failing:
                log("Jellyfin is reachable again")
            failing = False
        except Exception as exc:
            if not failing:
                log(f"can't check Jellyfin: {exc}")
            failing = True
        time.sleep(POLL)


if __name__ == "__main__":
    main()
