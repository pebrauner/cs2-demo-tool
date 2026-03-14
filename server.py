import asyncio
import io
import json
import os
import urllib.request

import requests
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

# demo_parser / demoparser2 are NOT imported at startup — they pull in
# polars, pyarrow, pandas, scipy etc. which together exceed the 512 MB
# free-tier limit before any request is even handled.
# Instead, import lazily inside load_demo() the first time it is called.

# ── Map coordinate configs (from cs2dave frontend source) ──────────────────────
# Formula: pixel_x = (world_x - pos_x) / scale
#          pixel_y = (pos_y  - world_y) / scale   ← y-axis is flipped
MAP_CONFIGS = {
    "de_ancient": {"pos_x": -2953, "pos_y": 2164, "scale": 5},
    "de_dust2":   {"pos_x": -2476, "pos_y": 3239, "scale": 4.4},
    "de_mirage":  {"pos_x": -3230, "pos_y": 1713, "scale": 5},
    "de_inferno": {"pos_x": -2087, "pos_y": 3870, "scale": 4.9},
    "de_nuke":    {"pos_x": -3453, "pos_y": 2887, "scale": 7},
    "de_vertigo": {"pos_x": -3168, "pos_y": 1762, "scale": 4},
    "de_anubis":  {"pos_x": -2796, "pos_y": 3328, "scale": 5.22},
}

MAP_SIZE  = 1024  # map images are 1024×1024
DEMOS_DIR = "demos"

os.makedirs("cache",    exist_ok=True)
os.makedirs("maps",     exist_ok=True)
os.makedirs(DEMOS_DIR,  exist_ok=True)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://pedrobrauner.com", "http://pedrobrauner.com"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Sample demo auto-download ──────────────────────────────────────────────────
# On a fresh Render deploy the demos/ folder is empty. Download the sample demo
# in the background so visitors can use the tool without uploading anything.
SAMPLE_DEMO_URL  = "https://github.com/pebrauner/cs2-demo-tool/releases/download/sample-demo/sample.dem"
SAMPLE_DEMO_NAME = "sample.dem"

async def _download_sample_demo():
    if os.listdir(DEMOS_DIR):
        return  # already have demos, skip
    dest = os.path.join(DEMOS_DIR, SAMPLE_DEMO_NAME)
    try:
        print(f"[startup] Downloading sample demo…")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: urllib.request.urlretrieve(SAMPLE_DEMO_URL, dest))
        print(f"[startup] Sample demo ready: {SAMPLE_DEMO_NAME}")
    except Exception as e:
        print(f"[startup] Could not download sample demo: {e}")

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(_download_sample_demo())

# ── Data loading ──────────────────────────────────────────────────────────────

def load_demo(demo_name: str) -> dict:
    """
    Parse a local .dem file from the demos/ folder (with disk cache).
    demo_name is the bare filename, e.g. 'match.dem'.
    """
    dem_path = os.path.join(DEMOS_DIR, demo_name)
    if not os.path.exists(dem_path):
        raise HTTPException(404, f"Demo file '{demo_name}' not found in {DEMOS_DIR}/")
    # Lazy import — demoparser2 + its deps (polars/pyarrow/pandas) load here,
    # not at server startup, to stay within the 512 MB free-tier RAM limit.
    from demo_parser import parse_demo_cached
    return parse_demo_cached(dem_path)


def ensure_map_image(map_name: str) -> str:
    """Download map PNG from cs2dave if not already cached, return local path."""
    path = os.path.join("maps", f"{map_name}.png")
    if not os.path.exists(path):
        r = requests.get(f"https://cs2dave.com/maps/{map_name}.png", timeout=30)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    return path


# ── Coordinate helpers ─────────────────────────────────────────────────────────

def world_to_pixel(wx, wy, cfg):
    px = (wx - cfg["pos_x"]) / cfg["scale"]
    py = (cfg["pos_y"] - wy) / cfg["scale"]
    return px, py


# ── API routes ─────────────────────────────────────────────────────────────────

@app.get("/api/demos/list")
def list_demos():
    """Return .dem filenames available in the demos/ folder."""
    files = sorted(
        f for f in os.listdir(DEMOS_DIR) if f.lower().endswith(".dem")
    )
    return {"demos": files}


@app.post("/api/demos/upload")
async def upload_demo(file: UploadFile = File(...)):
    """Save an uploaded .dem file to the demos/ folder."""
    if not file.filename.lower().endswith(".dem"):
        raise HTTPException(400, "Only .dem files are accepted.")
    dest = os.path.join(DEMOS_DIR, os.path.basename(file.filename))
    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)
    return {"filename": os.path.basename(file.filename), "bytes": len(content)}


@app.get("/api/demo/{demo_name:path}")
def get_demo_info(demo_name: str):
    """
    Returns demo metadata: map, teams, round count, player list, per-round stats.
    Players keyed by Steam ID with { name, team (initial side) }.
    Extra fields vs Phase 1: adr, assists, kast (per player).
    """
    data = load_demo(demo_name)

    # Build player map from tick data (team = their side in round 1)
    players: dict[str, dict] = {}
    for rd in data["rounds"]:
        for tick_list in rd["ticks"].values():
            for p in tick_list:
                sid = p["steamid"]
                if sid not in players:
                    players[sid] = {"name": p["name"], "team": p["team"]}
        if len(players) >= 10:
            break

    # ── Per-player totals across all rounds ──────────────────────────────────
    kill_stats = {sid: {"kills": 0, "deaths": 0, "hs": 0, "assists": 0}
                  for sid in players}
    rounds_info  = []
    rounds_kills = []
    rounds_nades = []

    # For ADR: accumulate capped damage per (round, attacker, victim) → max 100
    # For KAST: track K/A/S/T flags per (round, player)
    adr_damage:   dict[str, float] = {sid: 0.0 for sid in players}
    rounds_played: dict[str, int]  = {sid: 0   for sid in players}
    kast_rounds:  dict[str, int]   = {sid: 0   for sid in players}

    for rd in data["rounds"]:
        rnum   = rd["round_num"]
        kills  = rd.get("kills",    [])
        damage = rd.get("damage",   [])
        ticks  = rd.get("ticks",    {})

        rounds_info.append({
            "round_num":  rnum,
            "winner":     rd.get("winner",     ""),
            "win_reason": rd.get("win_reason", ""),
        })

        # Kill / death / HS / assist totals
        for k in kills:
            a_sid  = k.get("attacker_steamid", "")
            v_sid  = k.get("victim_steamid",   "")
            hs     = bool(k.get("headshot", False))
            ast_sid = k.get("assister_steamid", "")
            if a_sid in kill_stats:
                kill_stats[a_sid]["kills"] += 1
                if hs:
                    kill_stats[a_sid]["hs"] += 1
            if v_sid in kill_stats:
                kill_stats[v_sid]["deaths"] += 1
            if ast_sid and ast_sid in kill_stats:
                kill_stats[ast_sid]["assists"] += 1

        # Per-round kill strip (for client-side K/D filtering)
        rk = [
            {
                "attacker_steamid": k.get("attacker_steamid", ""),
                "victim_steamid":   k.get("victim_steamid",   ""),
                "headshot":         bool(k.get("headshot", False)),
                "assister_steamid": k.get("assister_steamid", ""),
            }
            for k in kills
        ]
        rounds_kills.append({"round_num": rnum, "kills": rk})

        # Grenade counts per player this round
        rn_nades: dict[str, int] = {}
        for g in rd.get("grenades", []):
            sid = g.get("thrower_steamid", "")
            if sid in players:
                rn_nades[sid] = rn_nades.get(sid, 0) + 1
        rounds_nades.append({"round_num": rnum, "nades": rn_nades})

        # ADR: capped damage per victim per round
        # First find which players participated (appeared in any tick this round)
        in_round: set[str] = set()
        for tick_list in list(ticks.values())[:1]:
            for p in tick_list:
                in_round.add(p["steamid"])

        for sid in players:
            if sid in in_round:
                rounds_played[sid] += 1

        # Cap damage at 100 hp per victim per round per attacker
        dmg_cap: dict[tuple, int] = {}   # (attacker, victim) → total dealt so far
        for d in damage:
            a_sid = d.get("attacker_steamid", "")
            v_sid = d.get("victim_steamid",   "")
            amt   = int(d.get("dmg_health", 0))
            if a_sid not in players:
                continue
            key = (a_sid, v_sid)
            already = dmg_cap.get(key, 0)
            effective = min(amt, max(0, 100 - already))
            dmg_cap[key] = already + effective
            adr_damage[a_sid] = adr_damage.get(a_sid, 0.0) + effective

        # KAST flags
        killers_this_round: set[str]   = {k.get("attacker_steamid", "") for k in kills}
        assisters_this_round: set[str] = {k.get("assister_steamid", "") for k in kills
                                          if k.get("assister_steamid")}
        victims_this_round: dict[str, dict] = {
            k["victim_steamid"]: k for k in kills if k.get("victim_steamid")
        }

        # Survival: check last tick snapshot
        last_tick_alive: dict[str, bool] = {}
        if ticks:
            last_key = max(ticks.keys(), key=int)
            for p in ticks[last_key]:
                last_tick_alive[p["steamid"]] = p.get("is_alive", False)

        # Trade detection: victim X was traded if their killer Y was killed
        # within 5 s (320 ticks @64hz) by anyone on X's team
        TRADE_WINDOW = 320
        traded: set[str] = set()
        for victim_sid, kill_evt in victims_this_round.items():
            killer_sid = kill_evt.get("attacker_steamid", "")
            kill_tick  = kill_evt.get("tick", 0)
            victim_team = kill_evt.get("victim_team", "")
            for k2 in kills:
                if (k2.get("victim_steamid") == killer_sid
                        and kill_tick <= k2.get("tick", 0) <= kill_tick + TRADE_WINDOW
                        and k2.get("attacker_team") == victim_team):
                    traded.add(victim_sid)
                    break

        for sid in players:
            if sid not in in_round:
                continue
            k_flag = sid in killers_this_round
            a_flag = sid in assisters_this_round
            s_flag = last_tick_alive.get(sid, False)
            t_flag = sid in traded
            if k_flag or a_flag or s_flag or t_flag:
                kast_rounds[sid] += 1

    # Finalise ADR and KAST percentages
    adr  = {sid: round(adr_damage[sid] / rounds_played[sid], 1)
            if rounds_played[sid] > 0 else 0.0
            for sid in players}
    kast = {sid: round(kast_rounds[sid] / rounds_played[sid] * 100, 1)
            if rounds_played[sid] > 0 else 0.0
            for sid in players}

    return {
        "map_name":     data["map_name"],
        "team_ct":      data["team_ct"],
        "team_t":       data["team_t"],
        "tickrate":     data["tickrate"],
        "round_count":  len(data["rounds"]),
        "players":      players,
        "kill_stats":   kill_stats,
        "adr":          adr,
        "kast":         kast,
        "rounds_info":  rounds_info,
        "rounds_kills": rounds_kills,
        "rounds_nades": rounds_nades,
    }


@app.get("/api/heatmap/{demo_name:path}")
def get_heatmap(
    demo_name: str,
    steamid: str = Query(None),
    round_num: int = Query(None),
    side: str = Query(None),       # "CT" or "T"
    alive_only: bool = Query(True),
    sigma: float = Query(8.0),     # Gaussian blur radius in pixels
):
    """
    Returns a 1024×1024 PNG: the map image composited with a player-position heatmap.
    All filters are optional — omitting steamid shows all players.
    """
    data = load_demo(demo_name)
    map_name = data["map_name"]

    if map_name not in MAP_CONFIGS:
        raise HTTPException(400, f"Map '{map_name}' is not supported yet.")

    cfg = MAP_CONFIGS[map_name]
    xs, ys = [], []

    for rd in data["rounds"]:
        if round_num is not None and rd["round_num"] != round_num:
            continue
        freeze_end = rd.get("freeze_end_tick", 0)
        for tick_str, tick_list in rd["ticks"].items():
            if int(tick_str) < freeze_end:  # skip freeze phase positions
                continue
            for p in tick_list:
                if steamid and p["steamid"] != steamid:
                    continue
                if side and p["team"] != side:
                    continue
                if alive_only and not p["is_alive"]:
                    continue
                px, py = world_to_pixel(p["x"], p["y"], cfg)
                if 0 <= px < MAP_SIZE and 0 <= py < MAP_SIZE:
                    xs.append(px)
                    ys.append(py)

    if not xs:
        raise HTTPException(404, "No position data found for the given filters.")

    map_path = ensure_map_image(map_name)
    png = build_heatmap_png(xs, ys, map_path, sigma=sigma)
    return Response(content=png, media_type="image/png")


@app.get("/api/ticks/{demo_name:path}")
def get_ticks(demo_name: str, round_num: int = Query(1)):
    """
    Returns per-tick player positions (already converted to map pixel coords)
    for one round, plus kill/bomb-plant event markers for the scrubber.
    """
    data = load_demo(demo_name)
    map_name = data["map_name"]

    if map_name not in MAP_CONFIGS:
        raise HTTPException(400, f"Map '{map_name}' not supported")

    cfg      = MAP_CONFIGS[map_name]
    tickrate = data["tickrate"]
    rd       = next((r for r in data["rounds"] if r["round_num"] == round_num), None)
    if rd is None:
        raise HTTPException(404, f"Round {round_num} not found")

    start = rd["start_tick"]

    frames = []
    for tick_str, tick_players in sorted(rd["ticks"].items(), key=lambda x: int(x[0])):
        tick = int(tick_str)
        players = []
        for p in tick_players:
            px, py = world_to_pixel(p["x"], p["y"], cfg)
            players.append({
                "steamid":     p["steamid"],
                "name":        p["name"],
                "team":        p["team"],
                "px":          round(px, 1),
                "py":          round(py, 1),
                "health":      p["health"],
                "armor":       p.get("armor", 0),
                "is_alive":    p["is_alive"],
                "balance":     p.get("balance", 0),
                "weapon":      p.get("active_weapon", ""),
                "has_helmet":  p.get("has_helmet", False),
                "has_defuser": p.get("has_defuser", False),
                "yaw":         p.get("yaw", 0),
                "inventory":   p.get("inventory", []),
            })
        frames.append({
            "tick":  tick,
            "rel_s": round((tick - start) / tickrate, 3),
            "players": players,
        })

    kills = [
        {
            "rel_s":         round((k["tick"] - start) / tickrate, 2),
            "attacker_name": k.get("attacker_name", ""),
            "attacker_team": k.get("attacker_team", ""),
            "victim_name":   k.get("victim_name", ""),
            "victim_team":   k.get("victim_team", ""),
            "weapon":        k.get("weapon", ""),
            "headshot":      bool(k.get("headshot", False)),
        }
        for k in rd.get("kills", [])
    ]
    bomb_plants = [
        {"rel_s": round((b["tick"] - start) / tickrate, 2), "site": b.get("site", "")}
        for b in rd.get("bomb_events", []) if b.get("event_type") == "plant"
    ]

    return {
        "round_num":    round_num,
        "freeze_end_s": round((rd["freeze_end_tick"] - start) / tickrate, 2),
        "duration_s":   round((rd["end_tick"]        - start) / tickrate, 2),
        "frames":       frames,
        "kills":        kills,
        "bomb_plants":  bomb_plants,
    }


@app.get("/api/grenades/{demo_name:path}")
def get_grenades(demo_name: str, round_num: int = Query(None)):
    """
    Returns grenade events with pixel coords for throw/land positions and trajectory.
    Optionally filter by round_num.
    """
    data = load_demo(demo_name)
    map_name = data["map_name"]

    if map_name not in MAP_CONFIGS:
        raise HTTPException(400, f"Map '{map_name}' not supported")

    cfg      = MAP_CONFIGS[map_name]
    grenades = []

    for rd in data["rounds"]:
        if round_num is not None and rd["round_num"] != round_num:
            continue
        start    = rd["start_tick"]
        tickrate = data["tickrate"]
        for g in rd.get("grenades", []):
            throw_t    = g.get("throw_tick")
            detonate_t = g.get("detonate_tick")
            expire_t   = g.get("expire_tick")
            # Skip grenades with missing critical ticks
            if throw_t is None or detonate_t is None:
                continue
            # Fall back to detonate + 18 s for smokes if expire_tick is null
            if expire_t is None:
                expire_t = detonate_t + int(tickrate * 18)
            throw_px, throw_py = world_to_pixel(g["throw_x"], g["throw_y"], cfg)
            land_px,  land_py  = world_to_pixel(g["land_x"],  g["land_y"],  cfg)
            traj = None
            if g.get("trajectory"):
                traj = [
                    [round(world_to_pixel(pt[0], pt[1], cfg)[0], 1),
                     round(world_to_pixel(pt[0], pt[1], cfg)[1], 1)]
                    for pt in g["trajectory"]
                ]
            grenades.append({
                "round_num":       rd["round_num"],
                "grenade_type":    g["grenade_type"],
                "thrower_name":    g.get("thrower_name", ""),
                "thrower_steamid": g.get("thrower_steamid", ""),
                "throw_rel_s":     round((throw_t    - start) / tickrate, 2),
                "detonate_rel_s":  round((detonate_t - start) / tickrate, 2),
                "expire_rel_s":    round((expire_t   - start) / tickrate, 2),
                "throw_px":  round(throw_px, 1),
                "throw_py":  round(throw_py, 1),
                "land_px":   round(land_px,  1),
                "land_py":   round(land_py,  1),
                "trajectory": traj,
            })

    return {"grenades": grenades}


@app.get("/maps/{map_name}.png")
def serve_map(map_name: str):
    path = ensure_map_image(map_name)
    return FileResponse(path, media_type="image/png")


# ── Heatmap rendering ──────────────────────────────────────────────────────────

def build_heatmap_png(xs, ys, map_path: str, sigma: float = 8.0) -> bytes:
    """
    1. Build a 2D histogram from pixel positions
    2. Gaussian-smooth it
    3. Apply turbo colormap with custom transparency
    4. Alpha-composite over the map PNG
    5. Return raw PNG bytes
    """
    # Lazy imports — keep these heavy libs out of the global scope to save
    # startup memory on the free Render tier (512 MB limit).
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from scipy.ndimage import gaussian_filter

    # 1. Histogram
    grid = np.zeros((MAP_SIZE, MAP_SIZE), dtype=np.float32)
    xi = np.clip(np.round(xs).astype(int), 0, MAP_SIZE - 1)
    yi = np.clip(np.round(ys).astype(int), 0, MAP_SIZE - 1)
    np.add.at(grid, (yi, xi), 1)

    # 2. Smooth
    grid = gaussian_filter(grid, sigma=sigma)

    # 3. Normalise to [0, 1]
    if grid.max() > 0:
        grid /= grid.max()

    # 4. Colormap + alpha
    #    Using turbo: more perceptually vivid than jet (blue→cyan→green→yellow→red)
    cmap = plt.cm.turbo
    rgba = cmap(grid)  # shape (1024, 1024, 4), values 0..1

    # Custom alpha: power 0.25 makes low-density areas much more visible
    alpha = np.where(grid < 0.02, 0.0, np.power(grid, 0.25) * 0.88)
    rgba[..., 3] = alpha

    # Convert to uint8 PIL image
    overlay = Image.fromarray((rgba * 255).astype(np.uint8), "RGBA")

    # 5. Composite
    base = Image.open(map_path).convert("RGBA")
    result = Image.alpha_composite(base, overlay)

    buf = io.BytesIO()
    result.save(buf, "PNG")
    return buf.getvalue()


# ── Serve frontend ─────────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")
