"""dvr-guard: records live TV itself, and shares one tuner per channel between recordings and viewers.

Jellyfin's own recordings share the live-TV stream with anyone watching the same channel, and
Jellyfin closes that stream when the viewers leave, even while it's recording. It also drops
one-time recordings on guide refreshes. So dvr-guard takes recordings over:

- Tuner hub: it serves the tuner's channels as an M3U playlist (Jellyfin and admute use it as
  their tuner). Each channel is pulled from the tuner once, however many viewers and
  recordings use it, and reconnected if the stream drops.
- Recordings: you press Record in Jellyfin as usual. dvr-guard copies each scheduled
  recording into its own schedule and removes it from Jellyfin, then records it from the hub
  into Jellyfin's recording folder, with the same names and .nfo files, so it appears in the
  library. Viewers coming and going never affect it. Gaps (tuner trouble) are logged.
- A status page (behind the shared formlogin sign-in) lists what's scheduled, recording and
  done, and what each channel is serving.
"""

import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

import formlogin
import formlogin.httpserver


def need(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is not set (see .env.example)")
    return value


JELLYFIN = need("JELLYFIN_URL").rstrip("/")
API_KEY = need("JELLYFIN_API_KEY")
SOURCE_M3U = need("SOURCE_M3U")                    # the tuner's playlist
PUBLIC_URL = need("PUBLIC_URL").rstrip("/")         # how Jellyfin/admute reach this service
RECORD_DIR = os.environ.get("RECORD_DIR", "/recordings")  # Jellyfin's recording folder, mounted here
DATA = os.environ.get("DATA", "/data")
PORT = int(os.environ.get("PORT", "8000"))
POLL = float(os.environ.get("POLL_SECONDS", "15"))
LINGER = float(os.environ.get("LINGER", "10"))     # keep a channel tuned this long after its last user
EXTRA = timedelta(minutes=float(os.environ.get("EXTRA_MINUTES", "0")))  # added to every recording's end
GAP = 10                                            # seconds without data that count as a gap
MATCH = timedelta(hours=3)                          # how far a moved show is looked for in the guide
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def now():
    return datetime.now(timezone.utc)


def parse_time(s):
    """Jellyfin dates look like 2026-10-31T00:00:00.0000000Z."""
    if not s:
        return None
    s = re.sub(r"\.(\d{6})\d*", r".\1", s.replace("Z", "+00:00"))
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f0Z")


def local(t):
    return t.astimezone().strftime("%a %b %d %H:%M")


# --- Jellyfin API -------------------------------------------------------------

def api(method, path, params=None, body=None):
    url = JELLYFIN + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f'MediaBrowser Token="{API_KEY}"', "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if raw and "html" in resp.headers.get("Content-Type", ""):
            raise RuntimeError(f"{JELLYFIN} answered with a web page, not the API. If Jellyfin has a "
                               "base URL (Dashboard > Networking), add it to JELLYFIN_URL")
    return json.loads(raw) if raw else None


# --- the tuner's channels -----------------------------------------------------

_EXTINF = re.compile(r'#EXTINF:\s*-?\d+((?:\s+[\w-]+="[^"]*")*)\s*,(.*)')
_lineup = {"at": 0.0, "list": []}
_lineup_lock = threading.Lock()


def lineup():
    """Channels in SOURCE_M3U: number, name, attributes, stream URL (cached for 10 minutes)."""
    with _lineup_lock:
        if _lineup["list"] and time.monotonic() - _lineup["at"] < 600:
            return _lineup["list"]
        if SOURCE_M3U.startswith(("http://", "https://")):
            with urllib.request.urlopen(SOURCE_M3U, timeout=15) as resp:
                text = resp.read().decode("utf-8", "replace")
        else:
            with open(SOURCE_M3U, encoding="utf-8") as f:
                text = f.read()
        chans, info = [], None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#EXTINF"):
                m = _EXTINF.match(line)
                info = (m.group(1), m.group(2).strip()) if m else ("", "")
            elif line and not line.startswith("#") and info is not None:
                attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', info[0]))
                num = (attrs.get("tvg-chno") or attrs.get("channel-number") or attrs.get("channel-id")
                       or attrs.get("tvg-id") or str(len(chans) + 1))
                chans.append({"num": num, "name": attrs.get("tvg-name") or info[1], "attrs": info[0],
                              "title": info[1], "url": line})
                info = None
        _lineup.update(at=time.monotonic(), list=chans)
        return chans


def find_channel(num):
    return next((c for c in lineup() if c["num"] == str(num)), None)


# --- tuner hub ----------------------------------------------------------------

class Sub:
    """One user of a channel's stream: a viewer (HTTP client) or a recording."""

    def __init__(self, kind, label):
        self.kind, self.label = kind, label
        self.q = queue.Queue(maxsize=4000 if kind == "recording" else 1500)  # chunks of up to 64 KB
        self.synced, self.closed, self.since = False, False, time.time()

    def feed(self, data):
        if self.closed:
            return
        if not self.synced:  # start on a packet boundary
            i = next((i for i in range(min(188, len(data))) if all(
                data[i + k * 188] == 0x47 for k in range(3) if i + k * 188 < len(data))), None)
            if i is None:
                return
            data, self.synced = data[i:], True
        try:
            self.q.put_nowait(data)
        except queue.Full:
            if self.kind == "viewer":  # a viewer that stopped reading
                self.end()
            else:
                log(f"{self.label}: the disk can't keep up, dropping data")

    def end(self):
        self.closed = True
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass


class Channel:
    """One channel pulled from the tuner, shared by all its users."""

    def __init__(self, hub, chan):
        self.hub, self.num, self.name, self.url = hub, chan["num"], chan["name"], chan["url"]
        self.subs, self.lock = [], threading.Lock()
        self.thread, self.connected, self.idle_since, self.stop_flag = None, False, None, False
        self.resp = None

    def add(self, sub):
        with self.lock:
            self.subs.append(sub)
            self.idle_since = None
            self.stop_flag = False
            if self.thread is None:  # set back to None by run() under this lock when it exits
                self.thread = threading.Thread(target=self.run, daemon=True, name=f"tuner {self.num}")
                self.thread.start()

    def remove(self, sub):
        with self.lock:
            if sub in self.subs:
                self.subs.remove(sub)
            if not self.subs:
                self.idle_since = time.monotonic()

    def users(self):
        with self.lock:
            return list(self.subs)

    def has_recording(self):
        return any(s.kind == "recording" for s in self.users())

    def close(self, why):
        """Stop this channel and end its viewers (to free the tuner for a recording)."""
        log(f"{self.num} {self.name}: {why}")
        self.stop_flag = True
        for s in self.users():
            s.end()
        try:
            if self.resp:
                self.resp.close()
        except Exception:
            pass

    def run(self):
        backoff, failing = 1, False
        while True:
            with self.lock:
                idle = not self.subs and (self.idle_since is None or time.monotonic() - self.idle_since >= LINGER)
                if self.stop_flag or idle:
                    self.thread = None
                    leftover = [s for s in self.subs if s.kind == "viewer"]
                    break
            try:
                self.resp = urllib.request.urlopen(self.url, timeout=10)
                self.connected, backoff = True, 1
                if failing:
                    log(f"{self.num} {self.name}: tuner stream back")
                failing = False
                while not self.stop_flag:
                    data = self.resp.read1(65536)
                    if not data:
                        raise EOFError("the tuner ended the stream")
                    subs = self.users()
                    for s in subs:
                        s.feed(data)
                    with self.lock:
                        for s in [s for s in self.subs if s.closed and s.kind == "viewer"]:
                            self.subs.remove(s)
                        if not self.subs:
                            self.idle_since = self.idle_since or time.monotonic()
                            if time.monotonic() - self.idle_since >= LINGER:
                                break
            except urllib.error.HTTPError as exc:
                self.connected = False
                if exc.code == 503:  # all tuners busy
                    if self.has_recording() and self.hub.free_tuner_for(self):
                        continue
                    if not self.has_recording():
                        self.close("no free tuner")
                        continue
                if not failing:
                    log(f"{self.num} {self.name}: tuner said {exc.code}, retrying")
                failing = True
            except Exception as exc:
                self.connected = False
                if self.stop_flag:
                    break
                if not failing:
                    log(f"{self.num} {self.name}: tuner stream lost ({exc}), reconnecting")
                failing = True
            finally:
                try:
                    if self.resp:
                        self.resp.close()
                except Exception:
                    pass
                self.connected = False
            with self.lock:
                if self.stop_flag or not self.subs:
                    continue
            time.sleep(backoff)
            backoff = min(backoff * 2, 8)
        self.connected = False
        for s in leftover:
            s.end()


class Hub:
    def __init__(self):
        self.channels, self.lock = {}, threading.Lock()

    def channel(self, num):
        with self.lock:
            ch = self.channels.get(str(num))
            if ch is None:
                c = find_channel(num)
                if c is None:
                    return None
                ch = self.channels[c["num"]] = Channel(self, c)
            return ch

    def free_tuner_for(self, needy):
        """A recording needs a tuner and none is free: stop a channel that only has viewers."""
        with self.lock:
            victims = [c for c in self.channels.values() if c is not needy and c.connected
                       and c.users() and not c.has_recording()]
        if not victims:
            return False
        victims[0].close(f"stopped so {needy.num} {needy.name} can record")
        time.sleep(1)
        return True

    def status(self):
        with self.lock:
            chans = list(self.channels.values())
        out = []
        for c in chans:
            users = c.users()
            if users or c.connected:
                out.append({"num": c.num, "name": c.name, "connected": c.connected,
                            "viewers": sum(s.kind == "viewer" for s in users),
                            "recordings": [s.label for s in users if s.kind == "recording"]})
        return sorted(out, key=lambda c: c["num"])


HUB = Hub()


# --- recordings ---------------------------------------------------------------

def safe(name):
    return re.sub(r'[\\/:*?"<>|]+', " ", name).strip() or "Recording"


_folders = {}


def folders():
    """Jellyfin's recording folders, relative to its main one (RECORD_DIR here)."""
    if not _folders:
        cfg = api("GET", "/System/Configuration/livetv")
        root = (cfg.get("RecordingPath") or "").rstrip("/")

        def rel(p, default):
            p = (p or "").rstrip("/")
            return os.path.relpath(p, root) if root and p.startswith(root + "/") else default
        _folders.update(root=root, series=rel(cfg.get("SeriesRecordingPath"), ""),
                        movies=rel(cfg.get("MovieRecordingPath"), ""))
    return _folders


def recording_path(rec):
    """Where Jellyfin would have put it, so it lands in the same library folders."""
    f, name, start = folders(), safe(rec["name"]), parse_time(rec["start"])
    if rec.get("is_movie"):
        title = f"{name} ({rec['year']})" if rec.get("year") else name
        return os.path.join(RECORD_DIR, f["movies"], title, title + ".ts")
    file = f"{name} {start:%Y_%m_%d_%H_%M_%S}" + (f" - {safe(rec['episode'])}" if rec.get("episode") else "")
    return os.path.join(RECORD_DIR, f["series"], name, file + ".ts")


def write_nfo(rec, path):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    try:
        if rec.get("is_movie"):
            xml = (f"<movie>\n  <title>{escape(rec['name'])}</title>\n  <plot>{escape(rec.get('overview', ''))}</plot>\n"
                   + (f"  <year>{rec['year']}</year>\n" if rec.get("year") else "") + "</movie>\n")
        else:
            show = os.path.join(folder, "tvshow.nfo")
            if not os.path.exists(show):
                with open(show, "w", encoding="utf-8") as f:
                    f.write(f"<?xml version=\"1.0\" encoding=\"utf-8\" standalone=\"yes\"?>\n<tvshow>\n"
                            f"  <title>{escape(rec['name'])}</title>\n</tvshow>\n")
            xml = (f"<episodedetails>\n  <title>{escape(rec.get('episode') or rec['name'])}</title>\n"
                   f"  <aired>{parse_time(rec['start']).astimezone():%Y-%m-%d}</aired>\n"
                   f"  <plot>{escape(rec.get('overview', ''))}</plot>\n"
                   + "".join(f"  <genre>{escape(g)}</genre>\n" for g in rec.get("genres", [])) + "</episodedetails>\n")
        with open(path[:-3] + ".nfo", "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n' + xml)
    except OSError as exc:
        log(f"couldn't write the .nfo for {rec['name']}: {exc}")


def tell_jellyfin(path, kind):
    """Ask Jellyfin to pick up the file (its path as Jellyfin sees it)."""
    try:
        root = folders().get("root")
        if not root:
            return
        api("POST", "/Library/Media/Updated", body={"Updates": [
            {"Path": root + path[len(RECORD_DIR):], "UpdateType": kind}]})
    except Exception as exc:
        log(f"couldn't tell Jellyfin about {os.path.basename(path)}: {exc}")


class Store:
    """The schedule, in data/schedule.json."""

    def __init__(self):
        self.path, self.lock = os.path.join(DATA, "schedule.json"), threading.RLock()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.recs = json.load(f)
        except FileNotFoundError:
            self.recs = {}

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.recs, f, indent=1)
            os.replace(tmp, self.path)


STORE = None


def window(rec):
    start, end = parse_time(rec["start"]), parse_time(rec["end"])
    return start - timedelta(seconds=rec.get("pre", 0)), end + timedelta(seconds=rec.get("post", 0)) + EXTRA


def label(rec):
    return rec["name"] + (f" - {rec['episode']}" if rec.get("episode") else "")


def record(rec):
    """Write the channel's stream to the file until the slot ends, through the hub."""
    begin, stop = window(rec)
    ch = HUB.channel(rec["channel"])
    if ch is None:
        log(f"channel {rec['channel']} isn't in the tuner's lineup, can't record {label(rec)}")
        with STORE.lock:
            rec["status"] = "failed"
            STORE.save()
        return
    path = rec.get("path")
    if not path:
        try:
            path = rec["path"] = recording_path(rec)
        except Exception as exc:  # Jellyfin unreachable: still record, in a folder of its own
            log(f"couldn't ask Jellyfin for its recording folders ({exc}), recording to the top folder")
            path = rec["path"] = os.path.join(RECORD_DIR, safe(f"{label(rec)} {parse_time(rec['start']).astimezone():%Y-%m-%d %H.%M}") + ".ts")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_nfo(rec, path)
    sub = Sub("recording", label(rec))
    ch.add(sub)
    log(f"recording {label(rec)} on {ch.num} {ch.name} until {local(stop)} -> {path}")
    tell_jellyfin(path, "Created")
    last, gap_from = time.time(), None
    try:
        with open(path, "ab") as out:
            while now() < stop and rec["status"] == "recording":
                try:
                    data = sub.q.get(timeout=2)
                except queue.Empty:
                    data = b""
                if data is None:
                    break
                t = time.time()
                if data:
                    if gap_from is not None:
                        secs = round(t - gap_from)
                        rec.setdefault("gaps", []).append([datetime.fromtimestamp(gap_from, timezone.utc).isoformat(), secs])
                        log(f"{label(rec)}: stream back after a {secs}s gap")
                        gap_from = None
                        STORE.save()
                    out.write(data)
                    rec["bytes"] = rec.get("bytes", 0) + len(data)
                    last = t
                elif gap_from is None and t - last > GAP:
                    gap_from = last
                    log(f"{label(rec)}: no data for {GAP}s, waiting for the tuner")
    finally:
        ch.remove(sub)
        with STORE.lock:
            if gap_from is not None:
                rec.setdefault("gaps", []).append([datetime.fromtimestamp(gap_from, timezone.utc).isoformat(),
                                                   round(time.time() - gap_from)])
            if rec["status"] == "recording":
                rec["status"] = "done"
            rec["finished"] = iso(now())
            STORE.save()
    gaps = sum(g[1] for g in rec.get("gaps", []))
    log(f"finished {label(rec)}: {rec.get('bytes', 0) / 1e9:.2f} GB" + (f", {gaps}s missing" if gaps else ""))
    tell_jellyfin(path, "Modified")


def from_timer(t):
    """A schedule entry from a Jellyfin timer."""
    p = t.get("ProgramInfo") or {}
    ch = api("GET", f"/LiveTv/Channels/{t['ChannelId']}")
    num = ch.get("ChannelNumber") or ch.get("Number")
    return {
        "id": t["Id"], "status": "scheduled", "name": t.get("Name") or p.get("Name") or "Recording",
        "episode": p.get("EpisodeTitle") or "", "overview": t.get("Overview") or p.get("Overview") or "",
        "genres": p.get("Genres") or [], "is_movie": bool(p.get("IsMovie")), "year": p.get("ProductionYear"),
        "channel": str(num), "channel_name": t.get("ChannelName") or "", "channel_id": t.get("ChannelId"),
        "program_id": t.get("ProgramId"), "start": t["StartDate"], "end": t["EndDate"],
        "pre": t.get("PrePaddingSeconds", 0), "post": t.get("PostPaddingSeconds", 0), "added": iso(now()),
    }


def take_over():
    """Move every recording scheduled in Jellyfin into our schedule, and remove it there."""
    for t in api("GET", "/LiveTv/Timers").get("Items", []):
        if t.get("Status") in ("Cancelled", "Completed", "Error"):
            continue
        try:
            rec = from_timer(t)
        except Exception as exc:
            log(f"couldn't read Jellyfin's recording {t.get('Name')}: {exc}")
            continue
        with STORE.lock:
            same = next((r for r in STORE.recs.values() if r["channel"] == rec["channel"]
                         and r["status"] in ("scheduled", "recording")
                         and abs(parse_time(r["start"]) - parse_time(rec["start"])) < timedelta(minutes=2)), None)
        try:
            api("DELETE", f"/LiveTv/Timers/{t['Id']}")
        except Exception as exc:
            log(f"couldn't remove {rec['name']} from Jellyfin's schedule, leaving it to Jellyfin: {exc}")
            continue
        if same:
            continue
        with STORE.lock:
            STORE.recs[rec["id"]] = rec
            STORE.save()
        log(f"took over {label(rec)} on {rec['channel']} {rec['channel_name']}, {local(parse_time(rec['start']))}"
            + (" (Jellyfin was recording it)" if t.get("Status") == "InProgress" else ""))


def follow_guide():
    """If a scheduled show moved in the guide, move the recording with it."""
    for rec in [r for r in STORE.recs.values() if r["status"] == "scheduled" and r.get("channel_id")]:
        start = parse_time(rec["start"])
        if start - now() > timedelta(days=14):
            continue
        try:
            items = api("GET", "/LiveTv/Programs", {
                "channelIds": rec["channel_id"], "minStartDate": iso(start - MATCH),
                "maxStartDate": iso(start + MATCH), "limit": 200}).get("Items", [])
        except Exception:
            continue
        same = [p for p in items if p.get("Name") == rec["name"]
                and (not rec.get("episode") or p.get("EpisodeTitle") == rec["episode"])]
        same.sort(key=lambda p: abs(parse_time(p["StartDate"]) - start))
        if same and (same[0]["StartDate"] != rec["start"] or same[0]["EndDate"] != rec["end"]):
            p = same[0]
            if parse_time(p["StartDate"]) == start and parse_time(p["EndDate"]) == parse_time(rec["end"]):
                continue
            log(f"{label(rec)} moved in the guide: {local(start)} -> {local(parse_time(p['StartDate']))}")
            with STORE.lock:
                rec["start"], rec["end"] = p["StartDate"], p["EndDate"]
                STORE.save()


def scheduler():
    """Start recordings on time (and any that were running when the service stopped)."""
    threads = {}
    while True:
        with STORE.lock:
            for rid, rec in list(STORE.recs.items()):
                begin, stop = window(rec)
                if rec["status"] == "scheduled" and now() >= stop:
                    rec["status"] = "missed"
                    STORE.save()
                elif rec["status"] in ("scheduled", "recording") and begin <= now() < stop:
                    if rid not in threads or not threads[rid].is_alive():
                        rec["status"] = "recording"
                        STORE.save()
                        threads[rid] = threading.Thread(target=record, args=(rec,), daemon=True, name=rid)
                        threads[rid].start()
                elif rec["status"] == "recording" and now() >= stop and (rid not in threads or not threads[rid].is_alive()):
                    rec["status"] = "done"  # ended while the service was down
                    rec.setdefault("finished", iso(stop))
                    STORE.save()
                elif rec["status"] in ("done", "missed", "failed", "cancelled", "stopped"):
                    finished = parse_time(rec.get("finished")) or stop
                    if now() - finished > timedelta(days=30):
                        del STORE.recs[rid]
                        STORE.save()
        time.sleep(2)


def jellyfin_loop():
    failing, last_guide = False, 0.0
    while True:
        try:
            take_over()
            if time.monotonic() - last_guide > 600:
                follow_guide()
                last_guide = time.monotonic()
            if failing:
                log("Jellyfin is reachable again")
            failing = False
        except Exception as exc:
            if not failing:
                log(f"can't check Jellyfin: {exc}")
            failing = True
        time.sleep(POLL)


# --- web ----------------------------------------------------------------------

def public(method, path, headers, peer):
    """Jellyfin and admute use the playlist and streams directly, without signing in."""
    return (path == "/lineup.m3u" or path.startswith("/stream/")) and formlogin.direct_private(headers, peer)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in formlogin.SECURITY_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if formlogin.httpserver.handle(self, LOGIN, path):
            return
        if path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/lineup.m3u":
            lines = ["#EXTM3U"]
            for c in lineup():
                lines += [f"#EXTINF:-1{c['attrs']},{c['title']}", f"{PUBLIC_URL}/stream/{urllib.parse.quote(c['num'])}"]
            return self.send(200, ("\n".join(lines) + "\n").encode(), "audio/x-mpegurl")
        if path.startswith("/stream/"):
            return self.stream(urllib.parse.unquote(path[8:]))
        if path == "/api/status":
            with STORE.lock:
                recs = sorted(STORE.recs.values(), key=lambda r: r["start"])
            return self.send(200, {"recordings": recs, "channels": HUB.status(), "now": iso(now())})
        self.send(404, {"error": "not found"})

    def stream(self, num):
        ch = HUB.channel(num)
        if ch is None:
            return self.send(404, {"error": f"no channel {num}"})
        if self.command == "HEAD":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
            return
        sub = Sub("viewer", f"viewer {self.client_address[0]}")
        ch.add(sub)
        self.close_connection = True
        try:
            first = sub.q.get(timeout=15)
            if first is None:
                return self.send(503, {"error": "no free tuner"})
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            data = first
            while data is not None:
                self.wfile.write(data)
                data = sub.q.get(timeout=30)
        except queue.Empty:
            pass
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            sub.closed = True
            ch.remove(sub)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if formlogin.httpserver.handle(self, LOGIN, path):
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(415, {"error": "send JSON"})
        try:
            req = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad JSON"})
        if path == "/api/cancel":
            with STORE.lock:
                rec = STORE.recs.get(str(req.get("id")))
                if not rec or rec["status"] not in ("scheduled", "recording"):
                    return self.send(404, {"error": "nothing to cancel"})
                rec["status"] = "stopped" if rec["status"] == "recording" else "cancelled"
                rec["finished"] = iso(now())
                STORE.save()
            log(f"{label(rec)}: {rec['status']} on the status page")
            return self.send(200, rec)
        self.send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


LOGIN = None


def main():
    global STORE, LOGIN
    os.makedirs(DATA, exist_ok=True)
    STORE = Store()
    LOGIN = formlogin.Login.from_env(  # DVR_USERNAME, DVR_PASSWORD_HASH, DVR_AUTH
        "DVR", "dvr-guard", os.path.join(DATA, "session.key"), title="DVR", is_public=public,
        accent=("#2f6fb4", "#6aa7ec"),
        icon="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><circle cx='8' cy='8' r='6' fill='%23e0484f'/></svg>")
    try:
        log(f"{len(lineup())} channels from {SOURCE_M3U}; playlist at {PUBLIC_URL}/lineup.m3u")
    except Exception as exc:
        log(f"can't read SOURCE_M3U yet ({exc}); will retry when asked")
    threading.Thread(target=scheduler, daemon=True, name="scheduler").start()
    threading.Thread(target=jellyfin_loop, daemon=True, name="jellyfin").start()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
