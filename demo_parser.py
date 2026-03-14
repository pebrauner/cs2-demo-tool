"""
demo_parser.py — CS2 .dem parser using demoparser2.

Output format matches the cs2dave API JSON structure so server.py endpoints need
minimal changes.  Extra fields beyond cs2dave:
  - rounds[].damage    list of {attacker_steamid, victim_steamid, dmg_health, tick}
  - rounds[].kills[]   now also include assister_steamid / assister_name (when present)

Run as a CLI to inspect a demo:
    python demo_parser.py <demo.dem>           # parse + print summary
    python parser.py <demo.dem> --props   # list available event names / header
"""

import bisect
import json
import math
import os
from collections import defaultdict

from demoparser2 import DemoParser


# ── DataFrame compatibility helpers ───────────────────────────────────────────
# demoparser2 v0.41.x returns pandas DataFrames; newer versions return polars.
# These helpers normalise the API so the rest of the code doesn't care.

def _rows(df) -> list[dict]:
    """Return DataFrame rows as a list of plain dicts (pandas or polars)."""
    try:
        return df.to_dicts()           # polars
    except AttributeError:
        return df.to_dict("records")   # pandas

def _is_empty(df) -> bool:
    """Return True if the DataFrame has no rows."""
    try:
        return df.is_empty()   # polars
    except AttributeError:
        return df.empty        # pandas

def _col_list(df, col: str) -> list:
    """Return a column as a plain Python list."""
    try:
        return df[col].to_list()   # polars
    except AttributeError:
        return df[col].tolist()    # pandas

# ── JSON safety ───────────────────────────────────────────────────────────────

def _sanitize_for_json(obj):
    """
    Recursively walk the parsed-demo dict and replace any NaN / ±Inf float
    (which pandas can emit for missing values) with 0.  This ensures the data
    is always serialisable to JSON without errors.
    """
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return 0
    return obj


# ── Constants ─────────────────────────────────────────────────────────────────

# Sample player positions every N ticks  (32 ticks ≈ 0.5 s at 64-tick)
TICK_INTERVAL = 32

# CS2 game-event name → grenade type string used in our output
GRENADE_EVENTS: dict[str, str] = {
    "hegrenade_detonate":    "he",
    "flashbang_detonate":    "flashbang",
    "smokegrenade_detonate": "smoke",
    "inferno_startburn":     "molotov",
    "decoy_started":         "decoy",
}

# ── Main entry point ──────────────────────────────────────────────────────────

def parse_demo(dem_path: str) -> dict:
    """
    Parse a CS2 .dem file and return a structured dict.
    Produces the same top-level shape as the cs2dave API response.
    """
    parser   = DemoParser(dem_path)
    header   = parser.parse_header()
    map_name = header.get("map_name", "de_unknown")
    tickrate = int(header.get("tick_rate", 64))

    rounds  = _get_rounds(parser, tickrate)
    players = _get_players(parser, rounds)
    t2r     = _build_tick_to_round(rounds)

    tick_data    = _get_player_ticks(parser, rounds)
    kills_data   = _get_kills(parser, t2r)
    damage_data  = _get_damage(parser, t2r)
    grenade_data = _get_grenades(parser, t2r)
    bomb_data    = _get_bomb_events(parser, t2r)

    team_ct, team_t = _derive_team_names(players)

    assembled = []
    for rd in rounds:
        rnum = rd["round_num"]
        assembled.append({
            "round_num":       rnum,
            "start_tick":      rd["start_tick"],
            "freeze_end_tick": rd["freeze_end_tick"],
            "end_tick":        rd["end_tick"],
            "winner":          rd["winner"],
            "win_reason":      rd["win_reason"],
            "ticks":           tick_data.get(rnum, {}),
            "kills":           kills_data.get(rnum, []),
            "damage":          damage_data.get(rnum, []),
            "grenades":        grenade_data.get(rnum, []),
            "bomb_events":     bomb_data.get(rnum, []),
        })

    return {
        "map_name":     map_name,
        "team_ct":      team_ct,
        "team_t":       team_t,
        "tickrate":     tickrate,
        "total_ticks":  int(header.get("total_ticks", 0)),
        "player_names": {sid: p["name"] for sid, p in players.items()},
        "rounds":       assembled,
    }


# ── Round boundary parsing ────────────────────────────────────────────────────

def _get_rounds(parser: DemoParser, tickrate: int = 64) -> list[dict]:
    """Return list of round dicts with start / freeze_end / end ticks and winner."""

    # --- freeze_end (one per real round, chronological) ---
    try:
        freeze_ticks = sorted(
            _col_list(parser.parse_event("round_freeze_end"), "tick")
        )
    except Exception:
        freeze_ticks = []

    # --- round_end — winner and reason arrive as strings from demoparser2.
    #     Filter out the warmup/bogus row (round=0, winner=NaN/empty). ---
    end_rows: list[dict] = []
    try:
        end_df = parser.parse_event("round_end", other=["winner", "reason"])
        for row in sorted(_rows(end_df), key=lambda r: r.get("tick", 0)):
            winner = row.get("winner")
            if winner is None:
                continue
            # pandas NaN: float that is not equal to itself
            try:
                if winner != winner:
                    continue
            except Exception:
                pass
            winner_str = str(winner).strip()
            if winner_str not in ("T", "CT"):
                continue           # still not a valid side — skip
            end_rows.append(row)
    except Exception:
        pass

    # --- round_start (optional — filter tick=1 warmup event) ---
    try:
        all_starts  = sorted(_col_list(parser.parse_event("round_start"), "tick"))
        start_ticks = [t for t in all_starts if t > 1]
    except Exception:
        start_ticks = []

    n_rounds = max(len(freeze_ticks), len(end_rows))
    if n_rounds == 0:
        return []

    rounds = []
    for i in range(n_rounds):
        rnum        = i + 1
        freeze_tick = freeze_ticks[i] if i < len(freeze_ticks) else 0
        end_row     = end_rows[i]     if i < len(end_rows)     else {}

        end_tick   = end_row.get("tick", freeze_tick + tickrate * 120) if end_row else freeze_tick + tickrate * 120
        # winner and reason are already strings ("T"/"CT", "ct_killed", etc.)
        winner_str = str(end_row.get("winner") or "CT").strip() if end_row else "CT"
        reason_str = str(end_row.get("reason") or "").strip()   if end_row else ""

        # round_start fires AFTER the previous round ends (i.e. start_ticks[j]
        # is the start of round j+2).  Round 1 has no explicit start event
        # (the warmup one was filtered), so we derive it from freeze_end.
        if i == 0:
            start_tick = max(1, freeze_tick - tickrate * 20)
        elif i - 1 < len(start_ticks):
            start_tick = start_ticks[i - 1]
        elif rounds:
            start_tick = rounds[-1]["end_tick"] + 1
        else:
            start_tick = max(1, freeze_tick - tickrate * 20)

        rounds.append({
            "round_num":       rnum,
            "start_tick":      start_tick,
            "freeze_end_tick": freeze_tick,
            "end_tick":        end_tick,
            "winner":          winner_str,
            "win_reason":      reason_str,
        })

    return rounds


def _build_tick_to_round(rounds: list[dict]):
    """Return a callable: tick (int) → round_num (int, 0 if unknown)."""
    if not rounds:
        return lambda _: 0

    starts = [rd["start_tick"] for rd in rounds]
    ends   = [rd["end_tick"]   for rd in rounds]
    nums   = [rd["round_num"]  for rd in rounds]

    def lookup(tick: int) -> int:
        idx = bisect.bisect_right(starts, tick) - 1
        if 0 <= idx < len(rounds) and tick <= ends[idx]:
            return nums[idx]
        return 0

    return lookup


# ── Player info ───────────────────────────────────────────────────────────────

def _get_players(parser: DemoParser, rounds: list[dict]) -> dict:
    """
    Build {steamid: {name, team, clan_name}} from the first few ticks of round 1.
    'team' is the player's starting side in round 1.
    """
    if not rounds:
        return {}

    r1 = rounds[0]
    # A handful of ticks near the start of live play
    sample = list(range(
        r1["freeze_end_tick"],
        min(r1["freeze_end_tick"] + TICK_INTERVAL * 30, r1["end_tick"]),
        TICK_INTERVAL * 10,
    ))
    if not sample:
        sample = [r1["freeze_end_tick"]]

    df = None
    for props in (
        ["name", "team_name", "clan_name"],
        ["name", "team_name"],
    ):
        try:
            df = parser.parse_ticks(props, ticks=sample)
            break
        except Exception:
            continue

    if df is None or _is_empty(df):
        return {}

    players: dict[str, dict] = {}
    for row in _rows(df):
        sid = str(row.get("steamid") or "")
        if not sid or sid == "0" or sid in players:
            continue
        team_raw = row.get("team_name") or ""
        players[sid] = {
            "name":      row.get("name") or "",
            "team":      "CT" if team_raw == "CT" else "T",
            "clan_name": row.get("clan_name") or "",
        }
    return players


def _derive_team_names(players: dict) -> tuple[str, str]:
    """Pick clan-level team names; fall back to 'CT' / 'T' if unavailable."""
    ct_clans: set[str] = set()
    t_clans:  set[str] = set()
    for p in players.values():
        clan = (p.get("clan_name") or "").strip()
        if clan:
            (ct_clans if p["team"] == "CT" else t_clans).add(clan)
    ct = max(ct_clans, key=len) if ct_clans else "CT"
    t  = max(t_clans,  key=len) if t_clans  else "T"
    return ct, t


# ── Player tick data ──────────────────────────────────────────────────────────

# Player props to sample every TICK_INTERVAL ticks.
# Note: "cash" and "weapons" are not exposed by demoparser2 v0.41.x via
# parse_ticks, so balance will always be 0 and inventory will be empty.
_TICK_PROPS = [
    "X", "Y", "Z", "yaw",
    "health", "armor_value",
    "has_helmet", "has_defuser",
    "active_weapon_name",    # currently held weapon (full name, e.g. "USP-S")
    "name", "team_name",
]
_TICK_PROPS_FALLBACK = [
    "X", "Y", "Z", "yaw",
    "health", "armor_value",
    "name", "team_name",
]


def _get_player_ticks(parser: DemoParser, rounds: list[dict]) -> dict:
    """
    Returns {round_num: {"tick_str": [player_dicts, …]}}
    sampled every TICK_INTERVAL ticks from freeze_end → round_end.
    Parsed in a single batched demoparser2 call for efficiency.
    """
    all_sample_ticks: list[int]      = []
    tick_to_rnum:     dict[int, int] = {}

    for rd in rounds:
        for t in range(rd["freeze_end_tick"], rd["end_tick"], TICK_INTERVAL):
            if t not in tick_to_rnum:
                all_sample_ticks.append(t)
                tick_to_rnum[t] = rd["round_num"]

    if not all_sample_ticks:
        return {}

    df = None
    for props in (_TICK_PROPS, _TICK_PROPS_FALLBACK):
        try:
            df = parser.parse_ticks(props, ticks=all_sample_ticks)
            break
        except Exception:
            continue

    if df is None or _is_empty(df):
        return {}

    result: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))

    for row in _rows(df):
        tick = row.get("tick", 0)
        rnum = tick_to_rnum.get(tick)
        if rnum is None:
            continue

        def _s(val, default=""):
            """Safe string: return default if val is None or NaN."""
            if val is None:
                return default
            if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                return default
            return str(val) if val else default

        def _f(val, default=0.0):
            """Safe float: return default if val is None or NaN/Inf."""
            try:
                f = float(val)
                return default if (math.isnan(f) or math.isinf(f)) else f
            except (TypeError, ValueError):
                return default

        sid      = _s(row.get("steamid"))
        health   = int(_f(row.get("health")))
        team_raw = _s(row.get("team_name"))
        # team_name is "CT" or "TERRORIST" in demoparser2
        team     = "CT" if team_raw == "CT" else "T"

        result[rnum][str(tick)].append({
            "steamid":       sid,
            "name":          _s(row.get("name")),
            "team":          team,
            "x":             _f(row.get("X")),
            "y":             _f(row.get("Y")),
            "z":             _f(row.get("Z")),
            "yaw":           _f(row.get("yaw")),
            "health":        health,
            "armor":         int(_f(row.get("armor_value"))),
            "is_alive":      health > 0,
            "active_weapon": _s(row.get("active_weapon_name")),
            "balance":       0,   # cash not available in demoparser2 parse_ticks
            "has_helmet":    bool(row.get("has_helmet") or False),
            "has_defuser":   bool(row.get("has_defuser") or False),
            "inventory":     [],  # weapons list not available in demoparser2 parse_ticks
        })

    return {rnum: dict(ticks) for rnum, ticks in result.items()}


# ── Kill events ───────────────────────────────────────────────────────────────

def _get_kills(parser: DemoParser, t2r) -> dict:
    """
    Returns {round_num: [kill_dicts]}.
    Each kill dict includes optional assister_steamid / assister_name.
    """
    try:
        df = parser.parse_event(
            "player_death",
            player=["X", "Y", "name", "steamid", "team_name"],
            other=["headshot", "weapon", "assister_steamid", "assister_name"],
        )
    except Exception:
        try:
            df = parser.parse_event(
                "player_death",
                player=["X", "Y", "name", "steamid", "team_name"],
                other=["headshot", "weapon"],
            )
        except Exception:
            return {}

    cols   = set(df.columns)
    result = defaultdict(list)

    for row in _rows(df):
        tick = row.get("tick", 0)
        rnum = t2r(tick)
        if rnum == 0:
            continue

        a_team = row.get("attacker_team_name") or ""
        v_team = row.get("user_team_name")     or ""

        kill: dict = {
            "tick":             tick,
            "attacker_steamid": str(row.get("attacker_steamid") or ""),
            "attacker_name":    row.get("attacker_name")    or "",
            "attacker_team":    "CT" if a_team == "CT" else "T",
            "attacker_x":       float(row.get("attacker_X") or 0.0),
            "attacker_y":       float(row.get("attacker_Y") or 0.0),
            "victim_steamid":   str(row.get("user_steamid") or ""),
            "victim_name":      row.get("user_name")    or "",
            "victim_team":      "CT" if v_team == "CT" else "T",
            "victim_x":         float(row.get("user_X") or 0.0),
            "victim_y":         float(row.get("user_Y") or 0.0),
            "weapon":           row.get("weapon")    or "",
            "headshot":         bool(row.get("headshot", False)),
        }

        assist_sid = str(row.get("assister_steamid") or "") if "assister_steamid" in cols else ""
        if assist_sid and assist_sid not in ("0", ""):
            kill["assister_steamid"] = assist_sid
            kill["assister_name"]    = row.get("assister_name") or ""

        result[rnum].append(kill)

    return dict(result)


# ── Damage events ─────────────────────────────────────────────────────────────

def _get_damage(parser: DemoParser, t2r) -> dict:
    """
    Returns {round_num: [damage_dicts]}.
    Used server-side to compute ADR.
    """
    try:
        df = parser.parse_event(
            "player_hurt",
            player=["steamid"],
            other=["dmg_health", "weapon"],
        )
    except Exception:
        return {}

    result = defaultdict(list)
    for row in _rows(df):
        tick = row.get("tick", 0)
        rnum = t2r(tick)
        if rnum == 0:
            continue
        result[rnum].append({
            "tick":             tick,
            "attacker_steamid": str(row.get("attacker_steamid") or ""),
            "victim_steamid":   str(row.get("user_steamid")     or ""),
            "dmg_health":       int(row.get("dmg_health")       or 0),
            "weapon":           row.get("weapon") or "",
        })

    return dict(result)


# ── Grenade events ────────────────────────────────────────────────────────────

def _get_grenades(parser: DemoParser, t2r) -> dict:
    """
    Returns {round_num: [grenade_dicts]}.
    Uses per-event parsing (detonation events + weapon_fire for throw positions).
    Note: parse_grenades() in demoparser2 v0.41.x returns per-tick trajectory
    rows (one row per grenade per tick), not per-throw rows — so we skip it
    and use the per-event fallback which gives exactly one row per grenade throw.
    """
    return _grenade_events_fallback(parser, t2r)


def _grenade_events_fallback(parser: DemoParser, t2r) -> dict:
    """
    Fallback grenade parsing: combine weapon_fire (throw pos) +
    detonation events (land pos).
    """
    # weapon_fire → collect throw positions keyed by (rnum, sid, nade_type)
    throw_pos: dict[tuple, dict] = {}
    try:
        wf_df = parser.parse_event(
            "weapon_fire",
            player=["X", "Y", "Z", "steamid", "name"],
            other=["weapon"],
        )
        _suffix_map = [
            ("hegrenade", "he"), ("flashbang", "flashbang"),
            ("smokegrenade", "smoke"), ("molotov", "molotov"),
            ("incendiary", "molotov"), ("decoy", "decoy"),
        ]
        for row in _rows(wf_df):
            weapon = (row.get("weapon") or "").lower()
            nade_type = next((nt for sfx, nt in _suffix_map if sfx in weapon), None)
            if not nade_type:
                continue
            tick = row.get("tick", 0)
            rnum = t2r(tick)
            if rnum == 0:
                continue
            sid = str(row.get("user_steamid") or "")
            key = (rnum, sid, nade_type)
            if key not in throw_pos:
                throw_pos[key] = {
                    "tick": tick,
                    "x":    float(row.get("user_X") or 0.0),
                    "y":    float(row.get("user_Y") or 0.0),
                    "z":    float(row.get("user_Z") or 0.0),
                    "name": row.get("user_name") or "",
                }
    except Exception:
        pass

    result = defaultdict(list)
    for event_name, nade_type in GRENADE_EVENTS.items():
        try:
            df = parser.parse_event(
                event_name,
                player=["steamid", "name"],   # gives user_steamid / user_name
                other=[],
            )
            for row in _rows(df):
                tick = row.get("tick", 0)
                rnum = t2r(tick)
                if rnum == 0:
                    continue

                sid   = str(row.get("user_steamid") or "")
                tinfo = throw_pos.get((rnum, sid, nade_type), {})

                # x/y/z (no prefix) is the grenade's world position at detonation
                result[rnum].append({
                    "grenade_type":    nade_type,
                    "thrower_steamid": sid,
                    "thrower_name":    tinfo.get("name") or row.get("user_name") or "",
                    "throw_tick":      tinfo.get("tick", tick),
                    "throw_x":         tinfo.get("x", 0.0),
                    "throw_y":         tinfo.get("y", 0.0),
                    "throw_z":         tinfo.get("z", 0.0),
                    "land_x":          float(row.get("x") or 0.0),
                    "land_y":          float(row.get("y") or 0.0),
                    "land_z":          float(row.get("z") or 0.0),
                    "detonate_tick":   tick,
                    "expire_tick":     None,
                    "trajectory":      [],
                })
        except Exception:
            continue

    return dict(result)


# ── Bomb events ───────────────────────────────────────────────────────────────

def _get_bomb_events(parser: DemoParser, t2r) -> dict:
    """Returns {round_num: [bomb_event_dicts]}."""
    result = defaultdict(list)

    for event_name, ev_type in [("bomb_planted", "plant"), ("bomb_defused", "defuse")]:
        try:
            df = parser.parse_event(
                event_name,
                player=["X", "Y", "steamid"],
                other=["site"],
            )
            for row in _rows(df):
                tick = row.get("tick", 0)
                rnum = t2r(tick)
                if rnum == 0:
                    continue

                site_raw = row.get("site") or ""
                if isinstance(site_raw, int):
                    site = "A" if site_raw == 0 else "B"
                else:
                    site = str(site_raw).strip().upper()
                    if site not in ("A", "B"):
                        site = "A"

                result[rnum].append({
                    "event_type":     ev_type,
                    "tick":           tick,
                    "site":           site,
                    "x":              float(row.get("user_X") or 0.0),
                    "y":              float(row.get("user_Y") or 0.0),
                    "player_steamid": str(row.get("user_steamid") or ""),
                })
        except Exception:
            continue

    return dict(result)


# ── Disk cache ────────────────────────────────────────────────────────────────

def parse_demo_cached(dem_path: str, cache_dir: str = "cache") -> dict:
    """
    Parse with disk cache.  Cache key = basename + file modification time,
    so editing/replacing the .dem file automatically invalidates the cache.
    """
    os.makedirs(cache_dir, exist_ok=True)
    base  = os.path.basename(dem_path)
    mtime = int(os.path.getmtime(dem_path))
    cache_path = os.path.join(cache_dir, f"{base}.{mtime}.json")

    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    data = _sanitize_for_json(parse_demo(dem_path))
    with open(cache_path, "w") as f:
        json.dump(data, f)
    return data


# ── CLI helper ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python demo_parser.py <demo.dem> [--props]")
        sys.exit(1)

    dem_path = sys.argv[1]
    p = DemoParser(dem_path)

    if "--props" in sys.argv:
        print("Header:", p.parse_header())
        try:
            print("Available game events:", p.list_game_events())
        except Exception as e:
            print("list_game_events() error:", e)
        sys.exit(0)

    print(f"Parsing {dem_path} …")
    result = parse_demo(dem_path)

    print(f"Map:      {result['map_name']}")
    print(f"Teams:    {result['team_ct']} (CT) vs {result['team_t']} (T)")
    print(f"Tickrate: {result['tickrate']}")
    print(f"Rounds:   {len(result['rounds'])}")

    if result["rounds"]:
        r1 = result["rounds"][0]
        print(f"\nRound 1:")
        print(f"  ticks:  {len(r1['ticks'])} samples")
        print(f"  kills:  {len(r1['kills'])}")
        print(f"  damage: {len(r1['damage'])}")
        print(f"  nades:  {len(r1['grenades'])}")
        print(f"  bomb:   {len(r1['bomb_events'])}")
        if r1["ticks"]:
            sample = next(iter(r1["ticks"].values()))
            if sample:
                print(f"\nSample player (round 1 first tick):")
                for k, v in sample[0].items():
                    if k != "inventory":
                        print(f"  {k}: {v}")
                print(f"  inventory: {sample[0].get('inventory', [])[:5]}")
