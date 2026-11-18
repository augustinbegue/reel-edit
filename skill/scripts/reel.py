#!/usr/bin/env python3
"""reel.py - helper for the reel-edit skill. Stdlib only; needs ffmpeg, the derush skill and (for
apply / verify / board) a running DaVinci Resolve.

It works in the SAME work dir as derush (it reads inventory.json and segments.json from it) and keeps
its own files under <work>/reel/.

Positions on the music are either a beat number (16, 16.5; beats are numbered by `audio`) or seconds
of the track written as a string ("7.52s"). Positions in the rushes are 0-based SOURCE FRAME OFFSETS,
exactly as in derush.

Commands (run `reel.py <cmd> -h` for options):
  selects    list the keeps and maybes of the derush step
  audio      find tempo, beats, bars and the drop of a track    -> <work>/reel/audio/<track>.json
  playbook   print the editing rules, the planned cuts and the reference reels
  check      validate <work>/reel/<name>.json and print the cut list
  apply      build the timeline in Resolve (music on A1, clips on V1, cut on the beat)
  verify     read the timeline back and measure every cut against the music
  board      storyboard sheets rendered from the built timeline -> <work>/sheets/reel_<name>_*.jpg
"""
import argparse, array, json, math, operator, os, re, statistics, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYBOOK = os.path.join(os.path.dirname(HERE), "references", "playbook.json")
TAG = "reel:"                      # customData prefix of every marker this tool owns
ROLES = {                          # role -> (shortest, longest) seconds before a warning, marker colour
    "hook":   (1.0, 6.5, "Red"),
    "shot":   (0.6, 2.5, "Blue"),
    "insert": (0.2, 1.0, "Pink"),
    "hold":   (2.0, 6.5, "Yellow"),
    "closer": (0.8, 4.0, "Green"),
}
FORMATS = ("letterbox", "rotate", "project")
VERTICAL = (1080, 1920)


def die(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- derush primitives
def load_derush():
    """The Resolve connection, media-pool walk, sheets and segment rules come from the derush skill."""
    cands = [os.environ.get("DERUSH_SCRIPTS"),
             os.path.join(os.path.dirname(os.path.dirname(HERE)), "derush", "scripts"),         # sibling skill
             os.path.expanduser("~/.claude/skills/derush/scripts"),
             os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
                          "..", "derush", "skill", "scripts")]                                    # sibling repo
    for c in cands:
        if c and os.path.exists(os.path.join(c, "derush.py")):
            sys.path.insert(0, os.path.abspath(c))
            import derush
            return derush
    die("derush.py not found. Install the derush skill next to this one, or set DERUSH_SCRIPTS to its scripts dir.")


def paths(args):
    if not args.work:
        die("pass --work <the derush work dir>")
    w = os.path.abspath(args.work)
    if not os.path.exists(os.path.join(w, "segments.json")):
        die("%s has no segments.json. Run the derush skill first; reel-edit works in the same work dir." % w)
    for sub in ("reel", "reel/audio", "sheets"):
        os.makedirs(os.path.join(w, sub), exist_ok=True)
    return w


def load_selects(w):
    """-> (inventory, {clip: [segment dicts with 'id' added]}) ; dies if segments.json is not valid."""
    d = load_derush()
    inv = d.load_inventory(w)
    data, errors, _ = d.load_segments(w, inv)
    if errors:
        die("segments.json is not valid (%s ...). Fix it with the derush skill first." % errors[0])
    for name, segs in data["clips"].items():
        for i, s in enumerate(segs, 1):
            s["id"] = "%s-%02d" % (os.path.splitext(name)[0][-4:], i)
    return inv, data["clips"]


# ---------------------------------------------------------------- selects
def note_moments(seg, fps):
    """Times a derush note or reason points at ("at 235-237s", "at 50s", "from 4s"), in seconds from
    the start of the clip -> [(from, to, text)]. These are the moments derush saw something wrong."""
    text = " ".join(x for x in (seg.get("note"), seg.get("reason")) if x)
    out = []
    for m in re.finditer(r"\b(at|from|until|till)\s+(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?\s?s\b", text):
        a = float(m.group(2))
        b = float(m.group(3)) if m.group(3) else a
        if m.group(1) == "from" and not m.group(3):
            b = seg["out"] / fps
        if m.group(1) in ("until", "till") and not m.group(3):
            a = seg["in"] / fps
        out.append((a - 0.5, b + 0.5, text))
    return out


def cmd_selects(args):
    w = paths(args)
    inv, clips = load_selects(w)
    rows = []
    for name, segs in clips.items():
        fps = inv["clips"][name]["fps"]
        for s in segs:
            if s["verdict"] != "reject" and s["rating"] >= args.min_rating:
                rows.append((name, fps, s))
    if args.order == "rating":
        rows.sort(key=lambda r: (-r[2]["rating"], r[0], r[2]["in"]))
    print("timeline %s fps. Positions are source frame offsets; ask derush for pictures:" % inv["timelineFps"])
    print("  python3 <derush skill>/scripts/derush.py --work %s sheet CLIP --ranges FROM-TO:STEP" % w)
    print()
    print("%-8s %-14s %-13s %6s  %-3s %-6s %-5s %s" % ("id", "clip", "frames", "len", "st", "scale", "verd", "label [tags]  note"))
    for name, fps, s in rows:
        print("%-8s %-14s %-13s %5.1fs  %-3s %-6s %-5s %s [%s]%s" % (
            s["id"], name, "f%d-%d" % (s["in"], s["out"]), (s["out"] - s["in"]) / fps, "*" * s["rating"],
            s["scale"], s["verdict"], s["label"], " ".join(s.get("tags", [])),
            ("  " + s["note"]) if s.get("note") else ""))
    tot = sum((s["out"] - s["in"]) / fps for _, fps, s in rows)
    by = {k: sum(1 for r in rows if r[2]["scale"] == k) for k in ("wide", "medium", "close", "detail")}
    print("\n%d selects, %.0f s.  By scale: %s.  3-star: %s" % (
        len(rows), tot, ", ".join("%s %d" % kv for kv in by.items()),
        ", ".join(r[2]["id"] for r in rows if r[2]["rating"] == 3) or "none"))


# ---------------------------------------------------------------- audio analysis
SR, HOP = 8000, 80                 # 10 ms hops
HOP_S = HOP / SR
LATENCY = 1.5                      # an onset measured at hop i sits at about (i + LATENCY) hops


def band_energies(path):
    """Three band-limited copies of the track (low / mid / high), energy per 10 ms hop."""
    fc = ("[0:a:0]aresample=%d,aformat=channel_layouts=mono,asplit=3[a][b][c];"
          "[a]lowpass=f=160[l];[b]highpass=f=160,lowpass=f=1600[m];[c]highpass=f=1600[h];"
          "[l][m][h]amerge=inputs=3[o]" % SR)
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-filter_complex", fc, "-map", "[o]",
                        "-f", "s16le", "-acodec", "pcm_s16le", "-"], capture_output=True)
    if r.returncode != 0 or len(r.stdout) < 6 * HOP * 100:
        die("ffmpeg could not decode at least 1 s of audio from %s: %s" % (path, r.stderr.decode()[-300:]))
    a = array.array("h")
    a.frombytes(r.stdout[:len(r.stdout) // 6 * 6])
    if sys.byteorder == "big":
        a.byteswap()
    n = len(a) // 3 // HOP
    mul = operator.mul
    bands = []
    for b in range(3):
        e = []
        for i in range(n):
            s = a[b + 3 * HOP * i: 3 * HOP * (i + 1): 3]
            e.append(sum(map(mul, s, s)) / HOP)
        bands.append(e)
    return bands, n


def onset_envelope(bands, n):
    """Half-wave rectified rise of log energy, per band, then summed. Returns (all, low-band only)."""
    per = []
    for e in bands:
        ref = 1e-3 * max(e) + 1.0
        c = [math.log1p((e[i] + e[min(i + 1, n - 1)]) / ref) for i in range(n)]      # 20 ms window
        per.append([0.0] + [max(0.0, c[i] - c[i - 1]) for i in range(1, n)])
    o = [per[0][i] + per[1][i] + per[2][i] for i in range(n)]
    # remove the slow floor (sustained sounds), keep the peaks
    k, acc, out = 50, 0.0, []
    pre = [0.0]
    for x in o:
        pre.append(pre[-1] + x)
    for i in range(n):
        lo, hi = max(0, i - k), min(n, i + k + 1)
        out.append(max(0.0, o[i] - (pre[hi] - pre[lo]) / (hi - lo)))
    sd = statistics.pstdev(out) or 1.0
    return [x / sd for x in out], per[0]


def fold(sig, period, n):
    """Mean of sig at each phase of the period (1-hop bins, lightly smoothed): a comb filter."""
    nb = int(math.ceil(period))
    acc, cnt = [0.0] * nb, [0] * nb
    for i in range(n):
        k = int(i % period)
        acc[k] += sig[i]; cnt[k] += 1
    f = [x / max(1, c) for x, c in zip(acc, cnt)]
    return [(f[i - 1] + f[i] + f[(i + 1) % nb]) / 3 for i in range(nb)]


def tempo_period(o, bpm=None):
    """Beat period in hops (float). Candidates come from the autocorrelation; each is scored by how
    well a comb at that period catches the onsets, weighted towards 120 BPM to pick the octave."""
    n = len(o)
    mul = operator.mul
    memo = {}

    def ac(L):
        L = int(L)
        if L not in memo:
            memo[L] = sum(map(mul, o, o[L:])) / max(1, n - L) if 0 < L < n - 10 else 0.0
        return memo[L]

    def peak_near(L, r):
        v, x = max((ac(x), x) for x in range(max(2, int(L - r)), int(L + r) + 1))
        a, c = ac(x - 1), ac(x + 1)
        den = a - 2 * v + c
        return x + (0.5 * (a - c) / den if den else 0.0)

    def refine(p):                              # a longer baseline pins the period down
        for mult in (4, 8, 16):
            if p * mult < n / 3:
                p = peak_near(p * mult, 3) / mult
        return p

    if bpm:
        p = 60.0 / bpm / HOP_S
        return refine(peak_near(p, max(1, p * 0.03)))
    lo, hi = int(60.0 / 210 / HOP_S), int(60.0 / 50 / HOP_S)
    peaks = sorted((L for L in range(lo, hi + 1) if ac(L) > ac(L - 1) and ac(L) >= ac(L + 1)), key=lambda L: -ac(L))[:8]
    cands = {int(round(L * m)) for L in peaks for m in (1, 2, 0.5) if lo <= L * m <= hi}
    mean_o = statistics.mean(o) or 1.0
    best = None
    for L in sorted(cands):
        p = refine(peak_near(L, 2))
        if not lo - 1 <= p <= hi + 1:
            continue
        v = max(fold(o, p, n)) / mean_o * math.exp(-0.5 * (math.log2(60.0 / (p * HOP_S) / 120.0) / 0.5) ** 2)
        if best is None or v > best[0]:
            best = (v, p)
    if not best:
        die("no tempo found. Pass --bpm.")
    return best[1]


def track_beats(o, period, tight=100.0):
    """Dynamic-programming beat tracker (Ellis 2007): follows the onsets, pays for leaving the tempo."""
    n = len(o)
    lo, hi = max(1, int(round(period / 2))), int(round(period * 2))
    pen = [0.0] * (hi + 1)
    for d in range(lo, hi + 1):
        pen[d] = -tight * math.log(d / period) ** 2
    cum, back = list(o), [-1] * n
    for t in range(lo, n):
        best, bi = -1e18, -1
        for d in range(lo, min(hi, t) + 1):
            v = cum[t - d] + pen[d]
            if v > best:
                best, bi = v, t - d
        cum[t] = o[t] + best
        back[t] = bi
    t = max(range(max(0, n - hi), n), key=lambda i: cum[i])
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    return beats[::-1]


def interp(o, x):
    i = int(math.floor(x))
    if i < 0 or i + 1 >= len(o):
        return 0.0
    f = x - i
    return o[i] * (1 - f) + o[i + 1] * f


def analyse(path, bpm=None, downbeat=None, per_bar=4, shift=0.0):
    bands, n = band_energies(path)
    dur = n * HOP_S
    o, low = onset_envelope(bands, n)
    period = tempo_period(o, bpm)
    raw = track_beats(o, period)
    if len(raw) < 8:
        die("fewer than 8 beats found in %s. Is it music? Pass --bpm if you know the tempo." % path)
    # steady tempo? fit one straight line through the tracked beats (drop the ends, which sit in silence)
    core = raw[2:-2] if len(raw) > 12 else raw
    ks = list(range(len(core)))
    mk, mt = statistics.mean(ks), statistics.mean(core)
    slope = sum((k - mk) * (t - mt) for k, t in zip(ks, core)) / sum((k - mk) ** 2 for k in ks)
    resid = statistics.pstdev(t - (mt + slope * (k - mk)) for k, t in zip(ks, core)) * HOP_S
    steady = resid < 0.025 and abs(slope / period - 1) < 0.02
    if steady:
        # Where in the period is the beat? The full mix often peaks on off-beat hats and stabs; the
        # bass band peaks on the kick. Trust the bass when it is clear about it.
        fo, fl = fold(o, period, n), fold(low, period, n)
        pa = max(range(len(fo)), key=lambda i: fo[i])
        pl = max(range(len(fl)), key=lambda i: fl[i])
        apart = min(abs(pa - pl), len(fo) - abs(pa - pl))
        clear = fl[pl] / (fl[pa] or 1e-9)
        if apart <= 3:
            ph, env, phase_how = pa, o, "bass and full mix agree"
        elif clear >= 1.3:
            ph, env, phase_how = pl, low, "taken from the bass; the full mix peaks %.2f beat away (hats or stabs off the beat)" % (apart / period)
        else:
            ph, env, phase_how = pa, o, "UNSURE: the bass peaks %.2f beat away from the full mix" % (apart / period)
        icpt = ph + 0.5

        def score(a, p):                         # fine-tune phase and period on the envelope (sub-hop)
            k0 = math.ceil(-a / p)
            return sum(interp(env, a + k * p) for k in range(k0, int((n - 1 - a) / p) + 1))
        for _ in range(2):
            icpt = max((icpt + d / 10.0 for d in range(-20, 21)), key=lambda a: score(a, period))
            period = max((period * (1 + d / 20000.0) for d in range(-20, 21)), key=lambda p: score(icpt, p))
        icpt += shift * period
        k0 = math.ceil((-LATENCY - icpt) / period)
        hops = [icpt + k * period for k in range(k0, int((n - 1 - icpt) / period) + 1)]
    else:
        phase_how = "follows the playing"
        hops = [h + shift * period for h in map(float, raw)]
        hops = [h for h in hops if 0 <= h < n - 1]
        period = statistics.median(b - a for a, b in zip(hops, hops[1:]))
    times = [round((h + LATENCY) * HOP_S, 4) for h in hops]
    m = len(times)

    def peak(sig, h, r=2):
        i = int(round(h))
        return max(sig[max(0, i - r): i + r + 1] or [0.0])
    hit = [peak(o, h) for h in hops]
    half = [peak(o, h + period / 2) for h in hops]
    lowhit = [peak(low, h) for h in hops]
    tot = [bands[0][i] + bands[1][i] + bands[2][i] for i in range(n)]
    pre = [0.0]
    for x in tot:
        pre.append(pre[-1] + x)

    def level(a, b):
        a, b = max(0, int(a)), min(n, int(b))
        return (pre[b] - pre[a]) / max(1, b - a)
    lev = [level(h, h + period) for h in hops]
    top = max(lev) or 1.0
    db = [round(10 * math.log10(max(x / top, 1e-6)), 1) for x in lev]

    # bar phase: the drop (largest jump in loudness) lands on a bar line; else the heaviest low hits do
    drop, jump = None, 0.0
    for k in range(per_bar, m - per_bar + 1):
        before = statistics.mean(db[k - per_bar:k])
        j = statistics.mean(db[k:k + per_bar]) - before
        if j > jump and min(db[k - per_bar:k]) > -30:      # music getting louder, not music starting after silence
            drop, jump = k, j
    by_phase = [statistics.mean(lowhit[p::per_bar]) for p in range(per_bar)]
    order = sorted(range(per_bar), key=lambda p: -by_phase[p])
    if downbeat is not None:
        phase, how = downbeat % per_bar, "given"
    elif drop is not None and jump >= 3.0:
        phase, how = drop % per_bar, "from the drop at b%d" % drop
    else:
        phase = order[0]
        margin = (by_phase[order[0]] - by_phase[order[1]]) / (by_phase[order[0]] or 1.0)
        how = "from the low hits" if margin >= 0.15 else "a guess (all beats hit alike)"
    # accents off the half-beat grid: where an insert or a whip can land
    thr = sorted(o)[int(n * 0.985)]
    accents = []
    for i in range(1, n - 1):
        if o[i] >= thr and o[i] >= o[i - 1] and o[i] > o[i + 1]:
            t = (i + LATENCY) * HOP_S
            k = min(range(m), key=lambda j: abs(times[j] - t))
            pos = k + (t - times[k]) / (period * HOP_S)
            if abs(pos * 2 - round(pos * 2)) * period * HOP_S / 2 > 0.04:
                accents.append({"t": round(t, 3), "pos": round(pos, 2), "hit": round(o[i], 1)})
    accents = sorted(sorted(accents, key=lambda a: -a["hit"])[:24], key=lambda a: a["t"])
    scale = sorted(hit)[int(m * 0.9)] or 1.0
    q = lambda x: min(9, int(round(9 * x / scale)))
    return {"file": os.path.abspath(path), "duration": round(dur, 3),
            "bpm": round(60.0 / (period * HOP_S), 2), "steady": steady, "jitterMs": round(resid * 1000, 1),
            "beatPhase": phase_how,
            "beatsPerBar": per_bar, "phase": phase, "phaseFrom": how,
            "drop": {"beat": drop, "jumpDb": round(jump, 1)} if drop is not None and jump >= 3.0 else None,
            "beats": [{"t": t, "hit": q(h), "half": q(x), "db": d} for t, h, x, d in zip(times, hit, half, db)],
            "accents": accents}


def audio_path(w, file_or_stem):
    stem = os.path.splitext(os.path.basename(file_or_stem))[0]
    return os.path.join(w, "reel", "audio", re.sub(r"[^\w.-]+", "_", stem) + ".json")


def print_audio(A, beats=None):
    B, pb, ph = A["beats"], A["beatsPerBar"], A["phase"]
    print("%s   %.1f s   %.1f BPM (%s)   %d beats" % (os.path.basename(A["file"]), A["duration"], A["bpm"],
          "steady grid" if A["steady"] else "tempo drifts, beats follow the playing, jitter %.0f ms" % A["jitterMs"], len(B)))
    print("beat position: %s%s" % (A["beatPhase"], "" if "agree" in A["beatPhase"] else
          ". If the beat markers sit between the kicks in Resolve, re-run with --shift 0.5"))
    print("bars of %d, bar lines %s%s" % (pb, A["phaseFrom"],
          "" if A["phaseFrom"] in ("given",) or A["phaseFrom"].startswith("from the drop") else
          " - not reliable, pass --downbeat BEAT if you know a bar start"))
    if A["drop"]:
        print("drop: b%d at %.2fs (+%.1f dB)" % (A["drop"]["beat"], B[A["drop"]["beat"]]["t"], A["drop"]["jumpDb"]))
    print("If the track feels twice as fast or slow as this BPM, re-run with --bpm (double or half).")
    print()
    if beats:
        lo, hi = beats
        print("beat    bar.n    time   hit  half   loud")
        for k in range(max(0, lo), min(len(B), hi + 1)):
            bar, pos = divmod(k - ph, pb)
            b = B[k]
            print("b%-5d %4d.%d %7.2fs   %d    %d   %5.1f dB" % (k, bar + 1, pos + 1, b["t"], b["hit"], b["half"], b["db"]))
    else:
        print("One line per bar. hits = onset strength 0-9 on each beat, halves = between the beats,")
        print("loud = level against the loudest bar. A beat number is the bar's first beat plus 0, 1, 2 ...")
        print()
        print(" bar  beats        time     loud              hits      halves")
        k = ph - pb if ph > 0 else 0
        while k < len(B):
            ks = [j for j in range(k, k + pb) if 0 <= j < len(B)]
            d = statistics.mean(B[j]["db"] for j in ks)
            bar = (ks[0] - ph) // pb + 1
            mark = "   <- DROP" if A["drop"] and A["drop"]["beat"] in ks else ""
            print("%4d  b%-4d-b%-4d %7.2fs  %5.1f dB %-8s  %-8s  %-8s%s" % (
                bar, ks[0], ks[-1], B[ks[0]]["t"], d, "#" * max(0, int(round(8 + d / 3))),
                " ".join(str(B[j]["hit"]) for j in ks), " ".join(str(B[j]["half"]) for j in ks), mark))
            k += pb
    if A["accents"]:
        print("\nstrong hits off the half-beat grid (an insert or a whip can land here):")
        print("  " + "   ".join("b%.2f %.2fs" % (a["pos"], a["t"]) for a in A["accents"]))


def cmd_audio(args):
    w = paths(args)
    if not os.path.exists(args.file):
        die("no such file: %s" % args.file)
    t = time.time()
    A = analyse(args.file, args.bpm, args.downbeat, args.beats_per_bar, args.shift)
    json.dump(A, open(audio_path(w, args.file), "w"))
    rng = None
    if args.beats:
        m = re.fullmatch(r"(\d+)-(\d+)", args.beats)
        if not m:
            die("--beats takes FROM-TO, e.g. 16-48")
        rng = (int(m.group(1)), int(m.group(2)))
    print_audio(A, rng)
    print("\nanalysed in %.1fs -> %s" % (time.time() - t, audio_path(w, args.file)))


# ---------------------------------------------------------------- playbook
def load_playbook():
    if not os.path.exists(PLAYBOOK):
        die("playbook missing: %s" % PLAYBOOK)
    return json.load(open(PLAYBOOK))


def refresh_playbook():
    """Re-read the shot plan from the page it was written on. The page embeds two JSON objects."""
    import urllib.request
    pb = load_playbook()
    html = urllib.request.urlopen(urllib.request.Request(pb["source"], headers={"User-Agent": "reel-edit"}), timeout=30).read().decode("utf-8")
    dec = json.JSONDecoder()
    i = html.find("const D=")
    if i < 0:
        die("the page at %s no longer embeds its data as `const D=`; playbook left unchanged." % pb["source"])
    D, end = dec.raw_decode(html[i + 8:])
    j = html.find("M=", i + 8 + end)
    M, _ = dec.raw_decode(html[j + 2:]) if 0 <= j - (i + 8 + end) < 8 else ({"clips": {}, "index": {"shots": {}}}, 0)
    rules = re.search(r"What the nine share</h3><ul[^>]*>(.*?)</ul>", html, re.S)
    if rules:
        pb["rules"] = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<li>(.*?)</li>", rules.group(1), re.S)]
    spec = D["spec"]
    pb["shots"] = {}
    for s in D["shots"]:
        if s.get("spec"):
            sp = dict(zip(spec, s["spec"]))
            pb["shots"][s["id"]] = {"title": s["title"], "block": s["block"], "camera": sp.get("Camera"), "lens": sp.get("Lens"),
                                    "move": sp.get("Move"), "car": sp.get("Car"), "tip": s.get("tip")}
    pb["cuts"] = [{"name": c["name"], "after": c["after"], "ids": c["ids"], "label": c.get("label", {})} for c in D["cuts"]]
    shot_of = {clip: sid for sid, clips in M["index"]["shots"].items() for clip in clips}
    pb["references"] = {}
    for key, r in D["reels"].items():
        known = sorted((c["a"], name.split("-", 1)[1], bool(c.get("slow")), shot_of.get(name))
                       for name, c in M["clips"].items() if c["reel"] == key)
        pb["references"][key] = {"who": r["who"], "url": r["url"], "name": r["name"], "pace": r["pace"], "steal": r["steal"],
                                 "shots": [{"n": n, "t": t, "slow": slow, "is": sid} for t, n, slow, sid in known]}
    pb["skip"] = [{"what": s["what"], "why": s["why"]} for s in D.get("skip", [])]
    pb["fetched"] = time.strftime("%Y-%m-%d")
    json.dump(pb, open(PLAYBOOK, "w"), indent=1, ensure_ascii=False)
    print("playbook refreshed from %s: %d shots, %d cuts, %d references\n" % (pb["source"], len(pb["shots"]), len(pb["cuts"]), len(pb["references"])))


def cmd_playbook(args):
    if args.refresh:
        refresh_playbook()
    pb = load_playbook()
    print("Shot plan and references from %s (read %s)\n" % (pb["source"], pb["fetched"]))
    print("WHAT THE REFERENCE REELS SHARE")
    for r in pb["rules"]:
        print("  - " + r)
    print("\nTHE PLANNED CUTS (edit order). A slot is a shot of the shot plan: id, what it shows, how it moves.")
    for c in pb["cuts"]:
        print("\n  %s   (after %s)" % (c["name"], c["after"]))
        for i, sid in enumerate(c["ids"]):
            s = pb["shots"].get(sid, {})
            lab = c.get("label", {}).get(str(i))
            print("    %2d  %-11s %-46s %s%s" % (i + 1, sid, s.get("title", "?"),
                  "; ".join(x for x in (s.get("move"), s.get("car")) if x), ("   [%s]" % lab) if lab else ""))
    print("\nTHE REFERENCES. Shot starts are seconds into the reference; only the shots the plan borrowed are")
    print("listed, so a missing number is a shot that is not in the plan. ~ = slow motion.")
    for key, r in pb["references"].items():
        print("\n  %s  %s  \"%s\"  %s" % (key, r["who"], r["name"], r["url"]))
        print("    pace: " + r["pace"])
        for s in r["steal"]:
            print("    idea: " + s)
        sh = [s for s in r["shots"] if s["n"].isdigit()]
        line = []
        for a, b in zip(sh, sh[1:] + [None]):
            ln = (" %.1fs" % (b["t"] - a["t"])) if b and int(b["n"]) == int(a["n"]) + 1 else ""
            line.append("#%s@%.2f%s%s%s" % (a["n"], a["t"], "~" if a["slow"] else "", ln, (" " + a["is"]) if a["is"] else ""))
        print("    shots: " + " | ".join(line))
    if pb.get("skip"):
        print("\nLEFT OUT ON PURPOSE")
        for s in pb["skip"]:
            print("  - %s: %s" % (s["what"], s["why"]))
    if args.shots:
        print("\nTHE SHOT PLAN")
        for sid, s in pb["shots"].items():
            print("  %-12s %-9s %-46s %s" % (sid, s["block"], s["title"], "; ".join(x for x in (s.get("camera"), s.get("move"), s.get("car")) if x)))


# ---------------------------------------------------------------- the edit
def edit_path(w, name):
    return os.path.join(w, "reel", name + ".json")


class Music:
    """Beat positions <-> seconds <-> timeline frames for one edit."""

    def __init__(self, A, fps):
        self.A, self.fps = A, fps
        self.t = [b["t"] for b in A["beats"]]
        self.origin = 0.0            # seconds of the track that sit on timeline frame 0

    def seconds(self, pos):
        if isinstance(pos, str):
            if pos == "end":
                return self.A["duration"]
            m = re.fullmatch(r"(-?\d+(?:\.\d+)?)s", pos.strip())
            if not m:
                raise ValueError("bad position %r. Use a beat number (16.5) or seconds as a string (\"7.52s\")" % pos)
            return float(m.group(1))
        if isinstance(pos, bool) or not isinstance(pos, (int, float)):
            raise ValueError("bad position %r" % (pos,))
        t, n = self.t, len(self.t)
        i = min(max(int(math.floor(pos)), 0), n - 2)
        return t[i] + (pos - i) * (t[i + 1] - t[i])

    def beat(self, sec):
        t, n = self.t, len(self.t)
        i = min(max(sum(1 for x in t if x <= sec) - 1, 0), n - 2)
        return i + (sec - t[i]) / (t[i + 1] - t[i])

    def frame(self, pos):
        return int(round((self.seconds(pos) - self.origin) * self.fps))

    def name(self, pos):
        return pos if isinstance(pos, str) else ("b%g" % pos)


def resolve_edit(w, name):
    """Load and validate an edit. -> (edit, errors, warnings, notes). Adds computed fields to the slots."""
    p = edit_path(w, name)
    if not os.path.exists(p):
        die("no %s. Write it first (see SKILL.md, step 5)." % p)
    try:
        E = json.load(open(p))
    except ValueError as x:
        die("%s is not valid JSON: %s" % (p, x))
    inv, clips = load_selects(w)
    fps = float(inv["timelineFps"])
    errors, warns, notes = [], [], []
    au = E.get("audio") or {}
    cands = [f for f in os.listdir(os.path.join(w, "reel", "audio")) if f.endswith(".json")]
    ap = audio_path(w, au["file"]) if au.get("file") else (os.path.join(w, "reel", "audio", cands[0]) if len(cands) == 1 else None)
    if not ap or not os.path.exists(ap):
        die("edit %s: audio.file %r has no analysis. Run `audio <file>` first%s." % (
            name, au.get("file"), "" if au.get("file") else " (several tracks analysed: name one in audio.file)"))
    A = json.load(open(ap))
    M = Music(A, fps)
    try:
        t_from, t_to = M.seconds(au.get("from", "0s")), M.seconds(au.get("to", "end"))
    except ValueError as x:
        die("edit %s: audio: %s" % (name, x))
    # the music is cut on a whole frame, and every cut is then rounded on its own: no error adds up
    a0 = int(round(t_from * fps))
    M.origin = a0 / fps
    total = int(round((t_to - M.origin) * fps))
    if not 0 <= t_from < t_to <= A["duration"] + 0.05:
        errors.append("audio: from %.2fs / to %.2fs must lie inside the track (0 - %.2fs)" % (t_from, t_to, A["duration"]))
    E["_"] = {"audio": A, "music": M, "fps": fps, "a0": a0, "total": total, "inv": inv, "clips": clips, "name": name}
    slots = E.get("slots") or []
    if not slots:
        errors.append("no slots")
    for i, s in enumerate(slots, 1):
        sid = "#%02d" % i
        try:
            s["_f"] = M.frame(s["at"])
        except (KeyError, ValueError) as x:
            errors.append("%s: at: %s" % (sid, x)); s["_f"] = None
    if any(s["_f"] is None for s in slots):
        return E, errors, warns, notes
    if slots and slots[0]["_f"] != 0:
        errors.append("#01 starts at frame %d: the first slot must start where the music starts (audio.from)" % slots[0]["_f"])
    used = {}
    for i, s in enumerate(slots, 1):
        sid = "#%02d" % i
        end = slots[i]["_f"] if i < len(slots) else total
        s["_n"] = n = end - s["_f"]
        role = s.get("role")
        if role not in ROLES:
            errors.append("%s: role must be one of %s" % (sid, ", ".join(ROLES)))
        if n < 3:
            errors.append("%s: lasts %d frames (starts at %s, next cut %s). Slots must follow each other in time and last 3 frames or more" % (
                sid, n, M.name(s["at"]), M.name(slots[i]["at"]) if i < len(slots) else "audio.to"))
            continue
        if not s.get("label"):
            errors.append("%s: label is empty" % sid)
        clip = s.get("clip")
        if clip not in clips:
            errors.append("%s: clip %r is not a derushed clip (%s)" % (sid, clip, ", ".join(clips))); continue
        cinfo = inv["clips"][clip]
        speed = float(s.get("speed", 100))
        if not 10 <= speed <= 800:
            errors.append("%s: speed %s%% is outside 10-800" % (sid, speed)); continue
        rate = speed / 100.0 * cinfo["fps"] / fps          # source frames consumed per timeline frame
        if ("src" in s) == ("sync" in s):
            errors.append("%s: give either src (first source frame) or sync {frame, at}, not both" % sid); continue
        if "sync" in s:
            try:
                d = M.frame(s["sync"]["at"]) - s["_f"]
                if not 0 <= d < n:
                    errors.append("%s: sync.at %s is outside the slot" % (sid, M.name(s["sync"]["at"]))); continue
                src = int(round(s["sync"]["frame"] - d * rate))
            except (KeyError, ValueError, TypeError) as x:
                errors.append("%s: sync: %s" % (sid, x)); continue
        else:
            src = s["src"]
        if not isinstance(src, int):
            errors.append("%s: src must be a whole source frame number" % sid); continue
        need = int(math.ceil(n * rate))
        s["_src"], s["_need"], s["_speed"], s["_rate"] = src, need, speed, rate
        if src < 0 or src + need > cinfo["frames"]:
            errors.append("%s: needs source f%d-f%d but %s has frames 0-%d" % (sid, src, src + need, clip, cinfo["frames"])); continue
        hit = [g for g in clips[clip] if g["in"] < src + need and g["out"] > src]
        bad = [g for g in hit if g["verdict"] == "reject"]
        if bad:
            g = bad[0]
            errors.append("%s: source f%d-f%d runs into a reject (%s f%d-f%d, %s). Stay inside f%d-f%d" % (
                sid, src, src + need, g["id"], g["in"], g["out"], g["label"],
                *next(((h["in"], h["out"]) for h in hit if h["verdict"] != "reject"), (0, 0))))
            continue
        s["_sel"] = main = max(hit, key=lambda g: min(g["out"], src + need) - max(g["in"], src))
        if any(g["verdict"] == "maybe" for g in hit):
            g = next(g for g in hit if g["verdict"] == "maybe")
            warns.append("%s: uses a maybe (%s: %s). Its flaw: %s. Usable only if the frames shown are free of that flaw; "
                         "never if the flaw is the car being cut, hidden or leaving the frame" % (
                             sid, g["id"], g["label"], g.get("note") or "see the derush note"))
        for g in hit:
            for a, b, text in note_moments(g, cinfo["fps"]):
                if a < (src + need) / cinfo["fps"] and b > src / cinfo["fps"]:
                    warns.append("%s: shows %.1f-%.1f s of %s, a moment the derush note points at (%s: %s). Look at these "
                                 "frames; if the car is cut, hidden or anything foreign is in frame, move the window" % (
                                     sid, src / cinfo["fps"], (src + need) / cinfo["fps"], clip, g["id"], text))
        lo, hi, _ = ROLES.get(role, (0, 99, ""))
        sec = n / fps
        if role in ROLES and not lo <= sec <= hi:
            warns.append("%s: a %s of %.2f s (usual: %.1f to %.1f s)" % (sid, role, sec, lo, hi))
        for j, a, b in used.get(clip, []):
            if a < src + need and b > src and not s.get("repeat"):
                warns.append("%s: shows frames already shown in #%02d (f%d-f%d). Mean it? Set \"repeat\": true" % (sid, j, max(a, src), min(b, src + need)))
        used.setdefault(clip, []).append((i, src, src + need))
        if i > 1 and slots[i - 2].get("clip") == clip and "_src" in slots[i - 2] and not s.get("repeat"):
            prev = slots[i - 2]
            gap = src - (prev["_src"] + prev["_need"])
            if abs(gap) < cinfo["fps"] and prev.get("_sel") is main:
                warns.append("%s: continues #%02d almost where it stopped (%+d frames): the cut will read as a glitch. Jump further or put another shot between" % (sid, i - 1, gap))
        # is the cut on the music?
        sec_at = M.seconds(s["at"])
        pos = M.beat(sec_at)
        beat_len = M.seconds(math.floor(pos) + 1) - M.seconds(math.floor(pos))
        off = abs(pos * 4 - round(pos * 4)) / 4 * beat_len
        near = min((abs(a["t"] - sec_at) for a in A["accents"]), default=9)
        s["_pos"] = pos
        if off > 0.04 and near > 0.04 and i > 1:
            warns.append("%s: the cut at %s is %.0f ms off the nearest quarter beat and on no listed accent" % (sid, M.name(s["at"]), off * 1000))
    ok = [s for s in slots if "_sel" in s]
    if ok and len(ok) == len(slots):
        secs = [s["_n"] / fps for s in slots]
        if slots[0].get("role") != "hook":
            warns.append("#01 is a %s: the first 2 s of a reel are a hook, never a plain car shot" % slots[0].get("role"))
        count = {}
        for s in slots:
            count[s["_sel"]["id"]] = count.get(s["_sel"]["id"], 0) + 1
        if max(count.values()) == 1 and len(slots) >= 6:
            notes.append("no select plays twice. The references play their best moving clip two or three times")
        run = 1
        for a, b in zip(slots, slots[1:]):
            run = run + 1 if a["_sel"]["scale"] == b["_sel"]["scale"] else 1
            if run == 4:
                notes.append("four %s shots in a row from #%02d: break the run with another scale if the selects allow" % (b["_sel"]["scale"], slots.index(b) - 2))
        three = sorted({g["id"] for segs in clips.values() for g in segs if g["verdict"] != "reject" and g["rating"] == 3} - set(count))
        if three:
            notes.append("3-star selects not used: " + ", ".join(three))
        if not 8 <= total / fps <= 32:
            notes.append("the edit runs %.1f s; the references run 12 to 26 s" % (total / fps))
        E["_"]["pace"] = "%d slots in %.1f s, median %.2f s, shortest %.2f s, longest %.2f s" % (
            len(slots), total / fps, statistics.median(secs), min(secs), max(secs))
    return E, errors, warns, notes


def print_edit(E):
    X = E["_"]
    M, fps = X["music"], X["fps"]
    print("%s   music %s  %.2fs-%.2fs  %.1f BPM   timeline %s fps, %d frames (%.2f s)" % (
        E.get("title", X["name"]), os.path.basename(X["audio"]["file"]), M.origin, M.origin + X["total"] / fps,
        X["audio"]["bpm"], fps, X["total"], X["total"] / fps))
    print(" #   at       frame   len    role    select   scale  st  speed  source         label")
    for i, s in enumerate(E.get("slots") or [], 1):
        if "_sel" not in s:
            print("%02d   %-8s (invalid, see errors)" % (i, M.name(s.get("at", "?"))))
            continue
        g = s["_sel"]
        print("%02d   %-8s f%-5d %5.2fs  %-7s %-8s %-6s %-3s %4.0f%%  f%d-%d  %s" % (
            i, M.name(s["at"]), s["_f"], s["_n"] / fps, s["role"], g["id"], g["scale"], "*" * g["rating"],
            s["_speed"], s["_src"], s["_src"] + s["_need"], s["label"]))


def cmd_check(args):
    w = paths(args)
    E, errors, warns, notes = resolve_edit(w, args.name)
    print_edit(E)
    if E["_"].get("pace"):
        print("\npace: " + E["_"]["pace"])
    for x in notes: print("NOTE: " + x)
    for x in warns: print("WARNING: " + x)
    for x in errors: print("ERROR: " + x)
    print("check: " + ("FAILED, fix %s" % edit_path(w, args.name) if errors else "OK" + (" (read the warnings: fix them or be able to say why not)" if warns else "")))
    sys.exit(1 if errors else 0)


# ---------------------------------------------------------------- apply
def pool_item_by_path(d, mp, path):
    for _, c in d.walk(mp.GetRootFolder()):
        if (c.GetClipProperty().get("File Path") or "") == path:
            return c
    return None


def timeline_by_name(project, name):
    for i in range(1, project.GetTimelineCount() + 1):
        if project.GetTimelineByIndex(i).GetName() == name:
            return project.GetTimelineByIndex(i)
    return None


def slot_name(i, s, M):
    return "%02d %s %s · %s" % (i, M.name(s["at"]), s["role"].upper(), s["label"])


def cmd_apply(args):
    w = paths(args)
    E, errors, warns, _ = resolve_edit(w, args.name)
    if errors:
        for x in errors: print("ERROR: " + x)
        die("the edit is not valid; run `check`.")
    d = load_derush()
    X = E["_"]
    M, fps, slots = X["music"], X["fps"], E["slots"]
    _, project, mp = d.connect()
    live = float(project.GetSetting("timelineFrameRate"))
    if abs(live - fps) > 0.01:
        die("the project now runs at %s fps but the derush inventory says %s. Re-run derush `inventory`." % (live, fps))
    fmt = args.format or E.get("format") or "letterbox"
    if fmt not in FORMATS:
        die("format must be one of %s" % ", ".join(FORMATS))
    items = d.media_clips(mp)
    for s in slots:
        if s["clip"] not in items:
            die("%s is no longer in the media pool" % s["clip"])
    applied_p = os.path.join(w, "reel", args.name + ".applied.json")
    if args.replace and os.path.exists(applied_p):
        old = timeline_by_name(project, json.load(open(applied_p)).get("timeline"))
        if old:
            print("replacing: %s" % old.GetName())
            mp.DeleteTimelines([old])
    folder = d.find_or_make_bin(mp, "Reel")
    mp.SetCurrentFolder(folder)
    # 1. the music, imported once
    apath = X["audio"]["file"]
    music = pool_item_by_path(d, mp, apath)
    if not music:
        got = mp.ImportMedia([apath])
        if not got:
            die("Resolve could not import %s" % apath)
        music = got[0]
    # 2. the timeline
    tname = d.unique_timeline_name(project, d.clean("%s - Reel - %s" % (project.GetName(), E.get("title") or args.name)))
    tl = mp.CreateEmptyTimeline(tname)
    if not tl:
        die("could not create timeline %s" % tname)
    if fmt != "project":
        ok = (tl.SetSetting("useCustomSettings", "1") and tl.SetSetting("timelineResolutionWidth", str(VERTICAL[0]))
              and tl.SetSetting("timelineResolutionHeight", str(VERTICAL[1])))
        if not ok:
            die("could not make timeline %s vertical" % tname)
    project.SetCurrentTimeline(tl)
    start = tl.GetStartFrame()
    # 3. A1: the music. Audio source positions are counted in timeline frames.
    got = mp.AppendToTimeline([{"mediaPoolItem": music, "startFrame": X["a0"], "endFrame": X["a0"] + X["total"],
                                "mediaType": 2, "trackIndex": 1, "recordFrame": start}])
    au = [i for i in (got or []) if i and i.GetStart() is not None]
    if len(au) != 1 or int(au[0].GetStart()) != start or int(au[0].GetDuration()) != X["total"]:
        die("the music did not land as asked (wanted %d frames at the timeline start)" % X["total"])
    fade = E.get("audio", {}).get("fadeOut")
    if fade:
        au[0].SetFades({"FadeIn": 0, "FadeOut": int(round(float(fade) * fps))})
    tl.SetTrackName("audio", 1, "Music")
    # 4. V1: one clip per slot, picture only, each at its own record frame. One at a time: a clip whose
    #    frame rate differs from the timeline's lands a frame long or short, so it is placed with a
    #    spare source frame and trimmed back to the exact length before the next one goes in.
    video = []
    for i, s in enumerate(slots, 1):
        cinfo = X["inv"]["clips"][s["clip"]]
        ratio = cinfo["fps"] / fps
        span = s["_n"] if abs(ratio - 1) < 1e-6 else int(math.ceil(s["_n"] * ratio)) + 1
        got = mp.AppendToTimeline([{"mediaPoolItem": items[s["clip"]], "startFrame": s["_src"],
                                    "endFrame": min(s["_src"] + span, cinfo["frames"]),
                                    "mediaType": 1, "trackIndex": 1, "recordFrame": start + s["_f"]}]) or []
        ti = got[0] if got and got[0] and got[0].GetStart() is not None else None
        if not ti:
            die("#%02d: Resolve did not place the clip at f%d" % (i, s["_f"]))
        if s["_speed"] != 100:
            if not ti.SetSpeed({"Percentage": float(s["_speed"]), "RippleTimeline": False}):
                die("#%02d: Resolve refused speed %s%%" % (i, s["_speed"]))
        extra = int(ti.GetDuration()) - s["_n"]
        if extra > 0:
            ti.SetRightOffset(ti.GetRightOffset(True) + extra)
        if int(ti.GetStart()) - start != s["_f"] or int(ti.GetDuration()) != s["_n"]:
            die("#%02d landed at f%d for %d frames, wanted f%d for %d" % (
                i, int(ti.GetStart()) - start, int(ti.GetDuration()), s["_f"], s["_n"]))
        video.append(ti)
    tl.SetTrackName("video", 1, "Reel")
    for i, (ti, s) in enumerate(zip(video, slots), 1):
        zoom = float(s.get("zoom", 1.0))
        if fmt == "rotate":
            # quarter turn; the long side of the picture fills the height of the vertical frame
            ti.SetProperty("RotationAngle", 90.0 if args.turn == "left" else -90.0)
            zoom *= VERTICAL[1] / float(VERTICAL[0])
        if zoom != 1.0:
            ti.SetProperty("ZoomX", zoom); ti.SetProperty("ZoomY", zoom)
        for key, prop in (("pan", "Pan"), ("tilt", "Tilt")):
            if s.get(key):
                ti.SetProperty(prop, float(s[key]))
        nm = d.clean(slot_name(i, s, M))
        if not ti.SetName(nm):
            die("Resolve refused the clip name %r" % nm)
        g = s["_sel"]
        ti.SetClipColor(d.SCALE_COLOR[g["scale"]])
        note = " | ".join(x for x in (s.get("note"), "%s %s f%d-%d" % (g["id"], s["clip"], s["_src"], s["_src"] + s["_need"]),
                                      "%g%%" % s["_speed"] if s["_speed"] != 100 else "", "%.2fs" % (s["_n"] / fps)) if x)
        tl.AddMarker(s["_f"], ROLES[s["role"]][2], nm, note, 1, TAG + "%s-%02d" % (args.name, i))
    # 5. a beat ruler on the music itself: clip markers travel with the audio clip
    for f, mk in (music.GetMarkers() or {}).items():
        if (mk.get("customData") or "").startswith(TAG):
            music.DeleteMarkerAtFrame(f)
    A = X["audio"]
    n_beats = 0
    for k, b in enumerate(A["beats"]):
        if M.origin - 0.01 <= b["t"] <= M.origin + X["total"] / fps:
            bar, pos = divmod(k - A["phase"], A["beatsPerBar"])
            n_beats += bool(music.AddMarker(int(round(b["t"] * fps)), "Red" if pos == 0 else "Cream",
                                            "b%d" % k, "bar %d beat %d" % (bar + 1, pos + 1), 1, TAG + "beat%d" % k))
    json.dump({"timeline": tname, "format": fmt, "appliedAt": time.strftime("%Y-%m-%d %H:%M:%S")}, open(applied_p, "w"), indent=1)
    print("timeline '%s' (%s): %d clips on V1, music on A1 from %.2fs, %d beat markers on the music clip" % (
        tname, "%dx%d %s" % (VERTICAL + (fmt,)) if fmt != "project" else "project format", len(video), M.origin, n_beats))
    for x in warns: print("WARNING (still open): " + x)
    print("apply: OK. Next: `verify`, then `board`.")


# ---------------------------------------------------------------- verify / board
def open_applied(w, name, project):
    p = os.path.join(w, "reel", name + ".applied.json")
    if not os.path.exists(p):
        die("edit %s was never applied" % name)
    tl = timeline_by_name(project, json.load(open(p))["timeline"])
    if not tl:
        die("timeline %s not found in the project" % json.load(open(p))["timeline"])
    return tl


def cmd_verify(args):
    w = paths(args)
    E, errors, _, _ = resolve_edit(w, args.name)
    if errors:
        die("the edit is not valid; run `check`.")
    d = load_derush()
    X = E["_"]
    M, fps, slots = X["music"], X["fps"], E["slots"]
    _, project, _ = d.connect()
    tl = open_applied(w, args.name, project)
    start, bad = tl.GetStartFrame(), 0
    au = tl.GetItemListInTrack("audio", 1) or []
    if len(au) != 1:
        print("MISMATCH: %d clips on A1, expected the music alone" % len(au)); bad += 1
    else:
        a = au[0]
        got = (int(a.GetStart()) - start, int(round(a.GetLeftOffset())), int(a.GetDuration()))
        if got != (0, X["a0"], X["total"]):
            print("MISMATCH: music is at f%d, from source frame %d, for %d frames; expected f0, %d, %d" % (got + (X["a0"], X["total"]))); bad += 1
    video = tl.GetItemListInTrack("video", 1) or []
    if len(video) != len(slots):
        print("MISMATCH: %d clips on V1, expected %d" % (len(video), len(slots))); bad += 1
    worst = 0.0
    print(" #   cut at    music position        off     clip")
    for i, (ti, s) in enumerate(zip(video, slots), 1):
        f, n = int(ti.GetStart()) - start, int(ti.GetDuration())
        speed = float((ti.GetSpeed() or {}).get("Percentage", 100.0))
        src = ti.GetLeftOffset(True) * speed / 100.0      # GetLeftOffset counts in timeline frames at the clip's speed
        name = ti.GetMediaPoolItem().GetName()
        want_src = s["_src"] * fps / X["inv"]["clips"][s["clip"]]["fps"]
        if f != s["_f"] or n != s["_n"] or name != s["clip"] or abs(src - want_src) > 1.01 or abs(speed - s["_speed"]) > 0.01:
            print("MISMATCH: #%02d is %s f%.0f at f%d for %d frames at %g%%; expected %s f%d at f%d for %d at %g%%" % (
                i, name, src, f, n, speed, s["clip"], s["_src"], s["_f"], s["_n"], s["_speed"])); bad += 1
        # what the ear gets: the picture changes at frame f, i.e. at this moment of the track
        heard = M.origin + f / fps
        off = (heard - M.seconds(s["at"])) * 1000
        worst = max(worst, abs(off))
        print("%02d   %6.2fs   %-8s = %7.3fs   %+5.0f ms   %s" % (i, f / fps, M.name(s["at"]), M.seconds(s["at"]), off, ti.GetName()))
    end = int(video[-1].GetEnd()) - start if video else 0
    if end != X["total"]:
        print("MISMATCH: picture ends at f%d, music at f%d" % (end, X["total"])); bad += 1
    half = 500.0 / fps
    print("timeline '%s': %d cuts, worst offset %.0f ms (half a frame is %.0f ms)" % (tl.GetName(), len(video), worst, half))
    if worst > half + 1:
        print("MISMATCH: a cut sits more than half a frame from its beat"); bad += 1
    print("verify: " + ("FAILED" if bad else "OK"))
    sys.exit(1 if bad else 0)


def cmd_board(args):
    w = paths(args)
    E, errors, _, _ = resolve_edit(w, args.name)
    if errors:
        die("the edit is not valid; run `check`.")
    d = load_derush()
    X = E["_"]
    M, slots = X["music"], E["slots"]
    _, project, _ = d.connect()
    tl = open_applied(w, args.name, project)
    for old in os.listdir(os.path.join(w, "sheets")):
        if old.startswith("reel_%s_" % args.name):
            os.remove(os.path.join(w, "sheets", old))
    frames, labels = [], []
    for i, s in enumerate(slots, 1):
        a, n = s["_f"], s["_n"]
        cells = [(a, "IN")] + ([(a + n // 2, "mid")] if n >= 6 else []) + [(a + n - 1, "OUT")]
        for f, tag in cells:
            frames.append(f); labels.append("#%02d %s %s" % (i, tag, M.name(s["at"]) if tag == "IN" else "f%d" % f))
    d.export_sheets(tl, frames, labels, os.path.join(w, "sheets", "reel_%s" % args.name), args.per_sheet, args.max_dim)
    print("Read left to right: each OUT cell is followed by the IN cell of the next shot. That pair is the cut.")
    print("First question for every cell: is the car whole and inside the frame? If not, that slot is wrong.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", help="the derush work dir (holds inventory.json and segments.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("selects"); p.add_argument("--min-rating", type=int, default=1)
    p.add_argument("--order", choices=("source", "rating"), default="rating"); p.set_defaults(fn=cmd_selects)
    p = sub.add_parser("audio"); p.add_argument("file")
    p.add_argument("--bpm", type=float, help="force the tempo (use it to double or halve a wrong guess)")
    p.add_argument("--downbeat", type=int, help="a beat number that starts a bar")
    p.add_argument("--shift", type=float, default=0.0, help="move every beat by this fraction of a beat (0.5 when the grid sits between the kicks)")
    p.add_argument("--beats-per-bar", type=int, default=4)
    p.add_argument("--beats", help="print one line per beat for beats FROM-TO instead of one line per bar")
    p.set_defaults(fn=cmd_audio)
    p = sub.add_parser("playbook"); p.add_argument("--shots", action="store_true", help="also list the whole shot plan")
    p.add_argument("--refresh", action="store_true", help="re-read the plan from the page it comes from"); p.set_defaults(fn=cmd_playbook)
    for name, fn in (("check", cmd_check), ("apply", cmd_apply), ("verify", cmd_verify), ("board", cmd_board)):
        p = sub.add_parser(name); p.add_argument("name", nargs="?", default="edit", help="edit name: <work>/reel/<name>.json")
        p.set_defaults(fn=fn)
        if name == "apply":
            p.add_argument("--format", choices=FORMATS, help="letterbox (default): 1080x1920, picture fitted to the width; "
                           "rotate: 1080x1920, picture turned a quarter and filling it; project: the project's own format")
            p.add_argument("--turn", choices=("left", "right"), default="left", help="with --format rotate: which way the picture is turned")
            p.add_argument("--replace", action="store_true", help="delete the timeline made by the previous apply of this edit")
        if name == "board":
            p.add_argument("--per-sheet", type=int, default=15); p.add_argument("--max-dim", type=int, default=2300)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
