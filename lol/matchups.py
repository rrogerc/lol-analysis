"""Thematic champion counters, synergies, and unsupervised draft archetypes.

Combines two signals from the ../lol-quant Master+ soloq pipeline and lol.db:
1. Residual L2-shrunk pairwise interaction weights (`lane-*.npz`) fitted on top
   of frozen `champion x role` main effects (`ctr`, `lane`, `syn`, `duo`, `jgl`).
2. Post-game behavioral telemetry from recent Master+ `participants` shards,
   static range/mobility metadata (`lol/data/builds/champions/`), and game-length
   scaling (`lol.db`) to form 13 standardized champion traits.

Champions are clustered strictly on their shrunk `[ctr, syn]` interaction vectors
(Ward hierarchical clustering on unit-normalized game-weighted profiles) so
archetypes reflect shared draft behavior rather than stat-sheet lookalikes, and
matched 1-to-1 to canonical archetype labels via linear sum assignment.
"""

import glob
import json
import os
import sys
from datetime import datetime, timezone

from lol import ladder
from web.db import BASE_DIR, db_connect, patch_key

MATCHUPS_DATA_DIR = os.path.join(BASE_DIR, "lol", "data", "matchups")
MATCHUPS_ARCHIVE = os.path.join(MATCHUPS_DATA_DIR, "matchups.json")
CHAMPIONS_DIR = os.path.join(BASE_DIR, "lol", "data", "builds", "champions")
KITS_DIR = os.path.join(BASE_DIR, "lol", "data", "builds", "kits")

ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
LANES = ("top", "jungle", "middle", "bottom", "support")
SLUG_FIXES = {"monkeyking": "wukong"}

TRAITS = [
    {"key": "magic_share", "label": "Magic damage", "short": "Magic",
     "desc": "Share of champion damage dealt as magic damage."},
    {"key": "true_share", "label": "True damage", "short": "True dmg",
     "desc": "Share of champion damage dealt as true damage."},
    {"key": "dpm", "label": "Damage output", "short": "DPM",
     "desc": "Post-mitigation damage to champions per minute."},
    {"key": "frontline", "label": "Frontline tanking", "short": "Frontline",
     "desc": "Damage taken plus self-mitigated damage per minute."},
    {"key": "sustain", "label": "Self sustain", "short": "Sustain",
     "desc": "Self-healing per minute across lane and fights."},
    {"key": "ally_peel", "label": "Ally peel & shields", "short": "Peel",
     "desc": "Healing and shielding granted to teammates per minute."},
    {"key": "hard_cc", "label": "Crowd control", "short": "Hard CC",
     "desc": "Crowd-control time applied to enemies per minute."},
    {"key": "range", "label": "Attack range", "short": "Range",
     "desc": "Basic attack range from static champion stats."},
    {"key": "mobility", "label": "Mobility", "short": "Mobility",
     "desc": "Dash, blink, and target-access mobility rating."},
    {"key": "solo_kill", "label": "Solo kill threat", "short": "Solo kills",
     "desc": "Average 1v1 solo kills per game."},
    {"key": "early_prio", "label": "Early lane prio", "short": "Early prio",
     "desc": "Early gold and XP lane advantage."},
    {"key": "sidelane", "label": "Turret & split push", "short": "Splitpush",
     "desc": "Turret damage per minute."},
    {"key": "late_scaling", "label": "Late scaling", "short": "Scaling",
     "desc": "35+ min win rate minus 0 to 20 min win rate."},
]

# Canonical archetype slots in functional order (Tanks -> Fighters -> Assassins
# -> Mages -> Marksmen -> Enchanters) so the 14x14 ecosystem matrix forms
# contiguous blocks. Cluster membership itself is 100% unsupervised; templates
# only name and order the discovered clusters via Hungarian matching.
ARCHETYPE_TEMPLATES = [
    {"name": "Frontline & Hook Tanks", "short": "Engage Tanks", "color": "--s1",
     "anchors": {"malphite", "ornn", "sion", "nautilus", "thresh", "blitzcrank",
                 "maokai", "sejuani", "amumu", "rammus", "drmundo", "tahmkench", "skarner"}},
    {"name": "Warden & Engage Peel", "short": "Warden Peel", "color": "--s2",
     "anchors": {"braum", "leona", "alistar", "rakan", "rell", "taric", "shen", "zac"}},
    {"name": "Lane-Bully Bruisers", "short": "Bully Bruisers", "color": "--s8",
     "anchors": {"aatrox", "darius", "renekton", "jax", "sett", "ksante",
                 "illaoi", "olaf", "urgot", "wukong", "poppy"}},
    {"name": "Ramp & Split Fighters", "short": "Split Fighters", "color": "--s3",
     "anchors": {"trundle", "nasus", "volibear", "udyr", "gwen", "gnar", "hecarim", "shyvana"}},
    {"name": "Early AD Divers", "short": "AD Divers", "color": "--s6",
     "anchors": {"leesin", "pantheon", "camille", "vi", "reksai", "warwick", "briar", "kled", "garen"}},
    {"name": "AD Assassins & Skirmishers", "short": "AD Skirmishers", "color": "--s6",
     "anchors": {"yasuo", "yone", "zed", "talon", "khazix", "rengar", "qiyana",
                 "naafiri", "viego", "irelia", "fiora", "riven", "masteryi", "nocturne", "ambessa"}},
    {"name": "AP Assassins & Burst", "short": "AP Assassins", "color": "--s5",
     "anchors": {"akali", "ekko", "katarina", "fizz", "leblanc", "diana",
                 "evelynn", "nidalee", "elise", "kassadin", "zoe"}},
    {"name": "AP Bruisers & Battlemages", "short": "AP Bruisers", "color": "--s5",
     "anchors": {"sylas", "mordekaiser", "swain", "rumble", "galio", "ryze", "singed", "chogath", "lillia"}},
    {"name": "Teamfight & Anti-Carry AP", "short": "Teamfight AP", "color": "--s5",
     "anchors": {"lissandra", "malzahar", "kennen", "fiddlesticks", "karthus",
                 "gragas", "ahri", "vex", "annie", "azir", "kayle", "vladimir"}},
    {"name": "Artillery & Control Mages", "short": "Control Mages", "color": "--s1",
     "anchors": {"xerath", "lux", "viktor", "syndra", "orianna", "hwei",
                 "velkoz", "ziggs", "anivia", "brand", "seraphine", "veigar", "cassiopeia", "taliyah"}},
    {"name": "Long-Range Marksmen", "short": "Lane Marksmen", "color": "--s3",
     "anchors": {"caitlyn", "jhin", "jinx", "ashe", "ezreal", "missfortune", "aphelios", "draven", "twitch"}},
    {"name": "Scaling & Caster Carries", "short": "Caster Carries", "color": "--s3",
     "anchors": {"smolder", "varus", "sivir", "kogmaw", "xayah", "zeri", "corki", "senna", "gangplank", "yunara"}},
    {"name": "Short-Range & Dive Carries", "short": "Dive Carries", "color": "--s8",
     "anchors": {"kaisa", "lucian", "vayne", "samira", "tristana", "kalista",
                 "graves", "kindred", "akshan", "jayce", "quinn"}},
    {"name": "Enchanters & Utility", "short": "Enchanters", "color": "--s2",
     "anchors": {"lulu", "nami", "milio", "soraka", "janna", "karma", "sona", "yuumi", "zilean", "bard", "ivern", "renata"}},
]


def _norm_slug(raw):
    s = raw.lower().replace(" ", "").replace("'", "").replace(".", "")
    return SLUG_FIXES.get(s, s)


def _champion_static(slug):
    """Resolve display name, attack range, and mobility rating from archived
    champion snapshots (`lol/data/builds/champions/` and `kits/`)."""
    name = None
    kit_path = os.path.join(KITS_DIR, f"{slug}.json")
    if os.path.exists(kit_path):
        try:
            with open(kit_path) as f:
                name = json.load(f).get("name")
        except (OSError, ValueError):
            pass

    atk_range = None
    mobility = None
    tags = []
    if os.path.isdir(CHAMPIONS_DIR):
        patches = sorted(
            (d for d in os.listdir(CHAMPIONS_DIR)
             if os.path.isdir(os.path.join(CHAMPIONS_DIR, d))),
            key=patch_key, reverse=True,
        )
        candidates = [slug, "monkeyking"] if slug == "wukong" else [slug]
        for p in patches:
            for cand in candidates:
                cdir = os.path.join(CHAMPIONS_DIR, p, cand)
                if not os.path.isdir(cdir):
                    continue
                dd_path = os.path.join(cdir, "ddragon.json")
                if os.path.exists(dd_path) and (name is None or atk_range is None):
                    try:
                        with open(dd_path) as f:
                            dd = json.load(f)
                        name = name or dd.get("name")
                        tags = tags or dd.get("tags", [])
                        if atk_range is None:
                            atk_range = dd.get("stats", {}).get("attackrange")
                    except (OSError, ValueError):
                        pass
                mk_path = os.path.join(cdir, "meraki.json")
                if os.path.exists(mk_path) and mobility is None:
                    try:
                        with open(mk_path) as f:
                            mk = json.load(f)
                        mobility = mk.get("attributeRatings", {}).get("mobility")
                        if atk_range is None:
                            atk_range = mk.get("stats", {}).get("attackRange", {}).get("flat")
                    except (OSError, ValueError):
                        pass
                if name is not None and atk_range is not None and mobility is not None:
                    break
            if name is not None and atk_range is not None and mobility is not None:
                break

    if mobility is None:
        mobility = 3 if "Assassin" in tags else 2 if "Fighter" in tags else 1
    return {
        "name": name or slug.capitalize(),
        "range": float(atk_range or 175.0),
        "mobility": float(mobility),
    }


def _scaling_deltas(con, idx_of):
    """Late-game (35+ min) minus early-game (0-20 min) win-rate delta from lol.db."""
    import numpy as np

    nc = len(idx_of)
    out = np.zeros(nc, dtype=np.float64)
    tier = ladder.default_tier(con)
    if not tier:
        return out
    rows = con.execute(
        "SELECT champion, bucket, SUM(games), SUM(wins) FROM stats "
        "WHERE tier=? GROUP BY champion, bucket",
        (tier,),
    )
    by_ch = {}
    for ch, b, g, w in rows:
        idx = idx_of.get(ch)
        if idx is not None:
            by_ch.setdefault(idx, {})[b] = (g, w)
    for idx, bk in by_ch.items():
        eg = sum(bk.get(b, (0, 0))[0] for b in ladder.EARLY_BUCKETS)
        ew = sum(bk.get(b, (0, 0))[1] for b in ladder.EARLY_BUCKETS)
        lg = sum(bk.get(b, (0, 0))[0] for b in ladder.LATE_BUCKETS)
        lw = sum(bk.get(b, (0, 0))[1] for b in ladder.LATE_BUCKETS)
        if eg >= 100 and lg >= 100:
            out[idx] = (lw / lg - ew / eg) * 100.0
    return out


def _telemetry_means(quant_dir, vocab, shards_per_region=400, platforms=None):
    """Aggregate recent Master+ participant shards into per-champion behavioral means."""
    import numpy as np
    import pyarrow.dataset as pads

    part_root = os.path.join(quant_dir, "data", "parquet", "participants")
    if not os.path.isdir(part_root):
        sys.exit(f"No participants parquet at {part_root}. Check --quant-dir.")

    regs = platforms or sorted(
        d for d in os.listdir(part_root) if os.path.isdir(os.path.join(part_root, d))
    )
    sample_files = []
    for reg in regs:
        p = os.path.join(part_root, reg)
        if os.path.isdir(p):
            fs = sorted(glob.glob(os.path.join(p, "*.parquet")))
            sample_files.extend(fs[-shards_per_region:] if shards_per_region else fs)
    if not sample_files:
        sys.exit(f"No parquet shards found under {part_root}.")

    cols = [
        "champion", "role", "game_duration",
        "damage_champs", "physical_to_champs", "magic_to_champs", "true_to_champs",
        "damage_taken", "self_mitigated", "total_heal",
        "heals_on_teammates", "shielded_on_teammates",
        "cc_time", "damage_to_turrets", "solo_kills", "early_lane_goldxp_adv",
    ]
    nc = len(vocab)
    c_lut = {v: i for i, v in enumerate(vocab)}
    cnt = np.zeros(nc, dtype=np.float64)
    # 10 raw telemetry columns:
    # 0 magic_share, 1 true_share, 2 phys_share, 3 dpm, 4 frontline,
    # 5 sustain, 6 ally_peel, 7 hard_cc, 8 solo_kill, 9 early_prio, 10 sidelane
    sums = np.zeros((nc, 11), dtype=np.float64)
    total_rows = 0

    batch_size = 600
    for start in range(0, len(sample_files), batch_size):
        batch = sample_files[start : start + batch_size]
        ds = pads.dataset(batch, format="parquet")
        tbl = ds.to_table(
            columns=cols,
            filter=(pads.field("game_duration") >= 600) & pads.field("role").isin(list(ROLES)),
        )
        if tbl.num_rows == 0:
            continue
        c_idx = np.array([c_lut.get(c, -1) for c in tbl["champion"].to_pylist()], dtype=np.int32)
        ok = c_idx >= 0
        if not np.any(ok):
            continue
        c_idx = c_idx[ok]
        total_rows += int(len(c_idx))

        dur_m = np.maximum(tbl["game_duration"].to_numpy(zero_copy_only=False)[ok] / 60.0, 1.0)
        dmg = np.maximum(tbl["damage_champs"].to_numpy(zero_copy_only=False)[ok].astype(np.float64), 1.0)
        phys = tbl["physical_to_champs"].to_numpy(zero_copy_only=False)[ok].astype(np.float64)
        mag = tbl["magic_to_champs"].to_numpy(zero_copy_only=False)[ok].astype(np.float64)
        tru = tbl["true_to_champs"].to_numpy(zero_copy_only=False)[ok].astype(np.float64)
        mit = (
            tbl["damage_taken"].to_numpy(zero_copy_only=False)[ok].astype(np.float64)
            + tbl["self_mitigated"].to_numpy(zero_copy_only=False)[ok].astype(np.float64)
        ) / dur_m
        self_heal = np.maximum(
            0.0,
            tbl["total_heal"].to_numpy(zero_copy_only=False)[ok].astype(np.float64)
            - tbl["heals_on_teammates"].to_numpy(zero_copy_only=False)[ok].astype(np.float64),
        ) / dur_m
        ally_peel = (
            tbl["heals_on_teammates"].to_numpy(zero_copy_only=False)[ok].astype(np.float64)
            + tbl["shielded_on_teammates"].to_numpy(zero_copy_only=False)[ok].astype(np.float64)
        ) / dur_m
        cc_pm = tbl["cc_time"].to_numpy(zero_copy_only=False)[ok].astype(np.float64) / dur_m
        turret_pm = tbl["damage_to_turrets"].to_numpy(zero_copy_only=False)[ok].astype(np.float64) / dur_m
        solo_k = tbl["solo_kills"].to_numpy(zero_copy_only=False)[ok].astype(np.float64)
        early_adv = np.nan_to_num(
            tbl["early_lane_goldxp_adv"].to_numpy(zero_copy_only=False)[ok].astype(np.float64),
            nan=0.0,
        )

        mat = np.column_stack([
            mag / dmg,
            tru / dmg,
            phys / dmg,
            dmg / dur_m,
            mit,
            self_heal,
            ally_peel,
            cc_pm,
            solo_k,
            early_adv,
            turret_pm,
        ])
        cnt += np.bincount(c_idx, minlength=nc)
        for k in range(mat.shape[1]):
            sums[:, k] += np.bincount(c_idx, weights=mat[:, k], minlength=nc)

    means = sums / np.maximum(cnt[:, None], 1.0)
    return means, total_rows


def build_matchups_payload(quant_dir, con, shards_per_region=400, platforms=None):
    """Build the self-contained matchups, traits, and archetypes payload."""
    try:
        import numpy as np
        from scipy.optimize import linear_sum_assignment
        from sklearn.cluster import AgglomerativeClustering
    except ModuleNotFoundError as e:
        sys.exit(f"Missing scientific dependency for matchups sync: {e.name}")

    models_dir = os.path.join(quant_dir, "data", "models")
    meta_path = os.path.join(models_dir, "meta.json")
    priors_path = os.path.join(models_dir, "role_priors.npy")
    if not os.path.exists(meta_path) or not os.path.exists(priors_path):
        sys.exit(f"No trained draft model metadata at {models_dir}. Run lol-quant draft train first.")

    with open(meta_path) as f:
        mmeta = json.load(f)
    vocab = mmeta["vocab"]
    slugs = [_norm_slug(v) for v in vocab]
    nc = len(vocab)
    idx_of = {s: i for i, s in enumerate(slugs)}

    cutoffs = [c for c in mmeta.get("cutoffs", [])
               if os.path.exists(os.path.join(models_dir, f"lane-{c}.npz"))]
    if not cutoffs:
        sys.exit(f"No lane-*.npz model found in {models_dir}.")
    cutoff = cutoffs[-1]
    mod = np.load(os.path.join(models_dir, f"lane-{cutoff}.npz"))
    coef = mod["coef"].astype(np.float64)
    role_counts = np.load(priors_path).astype(np.int64)  # (nc, 5)
    champ_counts = role_counts.sum(axis=1).astype(np.float64)
    p_champ = champ_counts / np.maximum(champ_counts.sum(), 1.0)

    n_main = 5 * nc
    n_pair = nc * (nc - 1) // 2
    iu, ju = np.triu_indices(nc, k=1)

    def sym_mat(vec):
        m = np.zeros((nc, nc), dtype=np.float64)
        m[iu, ju] = vec
        m[ju, iu] = vec
        return m

    def anti_mat(vec):
        m = np.zeros((nc, nc), dtype=np.float64)
        m[iu, ju] = vec
        m[ju, iu] = -vec
        return m

    # Convert log-odds weights to percentage-point win-rate deltas near 50% (* 25).
    w_main_pp = coef[:n_main].reshape(5, nc).T * 25.0  # (nc, 5)
    v_syn = coef[n_main : n_main + n_pair] * 25.0
    v_ctr = coef[n_main + n_pair : n_main + 2 * n_pair] * 25.0
    v_lane = coef[n_main + 2 * n_pair : n_main + 3 * n_pair] * 25.0
    v_duo = coef[n_main + 3 * n_pair : n_main + 4 * n_pair] * 25.0
    v_jgl = coef[n_main + 4 * n_pair : n_main + 5 * n_pair] * 25.0

    S_syn = sym_mat(v_syn)
    C_ctr = anti_mat(v_ctr)

    # Assemble 13 behavioral traits.
    t_means, telemetry_rows = _telemetry_means(
        quant_dir, vocab, shards_per_region=shards_per_region, platforms=platforms
    )
    statics = [_champion_static(s) for s in slugs]
    atk_range = np.array([st["range"] for st in statics], dtype=np.float64)
    mobility = np.array([st["mobility"] for st in statics], dtype=np.float64)
    scale_delta = _scaling_deltas(con, idx_of)

    X_raw = np.column_stack([
        t_means[:, 0],                 # 0: magic_share
        t_means[:, 1],                 # 1: true_share
        t_means[:, 3],                 # 2: dpm
        t_means[:, 4],                 # 3: frontline
        np.log1p(t_means[:, 5]),       # 4: sustain
        np.log1p(t_means[:, 6]),       # 5: ally_peel
        t_means[:, 7],                 # 6: hard_cc
        atk_range,                     # 7: range
        mobility,                      # 8: mobility
        t_means[:, 8],                 # 9: solo_kill
        t_means[:, 9],                 # 10: early_prio
        t_means[:, 10],                # 11: sidelane
        scale_delta,                   # 12: late_scaling
    ])
    w_mean = np.average(X_raw, axis=0, weights=p_champ)
    w_std = np.sqrt(np.average((X_raw - w_mean) ** 2, axis=0, weights=p_champ))
    X_std = (X_raw - w_mean) / np.maximum(w_std, 1e-8)

    # Signed DPM-weighted magic-vs-physical tilt: +1 ~ heavy AP carry, -1 ~ heavy AD carry,
    # ~0 ~ low-damage tank or enchanter (so Lulu/Braum aren't treated as AP carries).
    signed_dpm = (t_means[:, 0] - t_means[:, 2]) * t_means[:, 3]
    dmg_tilt = (signed_dpm - np.sum(p_champ * signed_dpm)) / 500.0

    # Per-champion team composition slopes (in pp) on ally dmg_tilt, frontline_z, ally_peel_z.
    ap_slope = np.zeros(nc, dtype=np.float64)
    front_slope = np.zeros(nc, dtype=np.float64)
    peel_slope = np.zeros(nc, dtype=np.float64)
    for i in range(nc):
        mask = p_champ > 0.001
        mask[i] = False
        idx = np.flatnonzero(mask)
        w = p_champ[idx] / p_champ[idx].sum()
        y = S_syn[i, idx] - np.sum(w * S_syn[i, idx])

        def slope_on(vec):
            xm = vec[idx] - np.sum(w * vec[idx])
            denom = np.sum(w * xm ** 2)
            return float(np.sum(w * xm * y) / denom) if denom > 1e-9 else 0.0

        ap_slope[i] = slope_on(dmg_tilt)
        front_slope[i] = slope_on(X_std[:, 3])
        peel_slope[i] = slope_on(X_std[:, 5])

    # Unsupervised Ward clustering on game-weighted [C_ctr, S_syn] interaction vectors.
    n_arch = len(ARCHETYPE_TEMPLATES)
    emb = np.hstack([C_ctr * np.sqrt(p_champ)[None, :], S_syn * np.sqrt(p_champ)[None, :]])
    emb_norm = emb / np.maximum(np.linalg.norm(emb, axis=1, keepdims=True), 1e-8)
    raw_labels = AgglomerativeClustering(n_clusters=n_arch, linkage="ward").fit_predict(emb_norm)

    # Match raw clusters 1-to-1 onto canonical ordered archetype templates.
    score_mat = np.zeros((n_arch, n_arch), dtype=np.float64)
    for raw_c in range(n_arch):
        members = [i for i in range(nc) if raw_labels[i] == raw_c]
        c_total = sum(p_champ[i] for i in members) or 1.0
        member_slugs = {slugs[i]: p_champ[i] / c_total for i in members}
        for t_idx, tmpl in enumerate(ARCHETYPE_TEMPLATES):
            score_mat[raw_c, t_idx] = sum(
                share for s, share in member_slugs.items() if s in tmpl["anchors"]
            )
    row_ind, col_ind = linear_sum_assignment(-score_mat)
    raw_to_canon = {int(r): int(c) for r, c in zip(row_ind, col_ind)}
    arch_of = np.array([raw_to_canon[int(c)] for c in raw_labels], dtype=np.int32)

    # Symmetric comp-adjusted kit synergy matrix for archetype rollups.
    S_kit = S_syn - 0.5 * (ap_slope[:, None] * dmg_tilt[None, :] + ap_slope[None, :] * dmg_tilt[:, None])
    np.fill_diagonal(S_kit, 0.0)

    archetypes = []
    for a in range(n_arch):
        tmpl = ARCHETYPE_TEMPLATES[a]
        members = sorted(
            (i for i in range(nc) if arch_of[i] == a),
            key=lambda i: (-champ_counts[i], slugs[i]),
        )
        m_weights = np.array([p_champ[i] for i in members], dtype=np.float64)
        m_weights = m_weights / np.maximum(m_weights.sum(), 1e-12)
        mean_z = np.sum(X_std[members] * m_weights[:, None], axis=0)
        # Pick top 4 defining traits by |z| (positive traits first for readability).
        top_idx = sorted(range(len(TRAITS)), key=lambda k: -abs(mean_z[k]))[:4]
        top_idx.sort(key=lambda k: (0 if mean_z[k] >= 0 else 1, -abs(mean_z[k])))
        sig = [[int(k), round(float(mean_z[k]), 2)] for k in top_idx]

        r_sums = role_counts[members].sum(axis=0).astype(np.float64)
        r_shares = (r_sums / np.maximum(r_sums.sum(), 1.0)).tolist()

        vs_ctr, vs_syn, vs_kit = [], [], []
        for b in range(n_arch):
            others = [j for j in range(nc) if arch_of[j] == b]
            num_ctr = num_syn = num_kit = den = 0.0
            for i in members:
                for j in others:
                    if i == j:
                        continue
                    wij = p_champ[i] * p_champ[j]
                    num_ctr += wij * C_ctr[i, j]
                    num_syn += wij * S_syn[i, j]
                    num_kit += wij * S_kit[i, j]
                    den += wij
            vs_ctr.append(round(num_ctr / den, 2) if den > 0 else 0.0)
            vs_syn.append(round(num_syn / den, 2) if den > 0 else 0.0)
            vs_kit.append(round(num_kit / den, 2) if den > 0 else 0.0)

        archetypes.append({
            "id": a,
            "name": tmpl["name"],
            "short": tmpl["short"],
            "color": tmpl["color"],
            "roles": [round(x, 3) for x in r_shares],
            "sig": sig,
            "members": [int(i) for i in members],
            "ctr": vs_ctr,
            "syn": vs_syn,
            "kitSyn": vs_kit,
        })

    champions = []
    for i in range(nc):
        champions.append({
            "slug": slugs[i],
            "name": statics[i]["name"],
            "arch": int(arch_of[i]),
            "roles": [int(x) for x in role_counts[i].tolist()],
            "main": [round(float(x), 2) for x in w_main_pp[i].tolist()],
            "traits": [round(float(x), 2) for x in X_std[i].tolist()],
            "dmgTilt": round(float(dmg_tilt[i]), 3),
            "apSlope": round(float(ap_slope[i]), 2),
            "frontSlope": round(float(front_slope[i]), 2),
            "peelSlope": round(float(peel_slope[i]), 2),
        })

    def quantize_centi_pp(vec):
        return [int(round(float(x) * 100.0)) for x in vec]

    n_train = next(
        (r["n_train"] for r in mmeta.get("report", [])
         if r.get("cutoff") == cutoff and r.get("model") == "lane" and "n_train" in r),
        mmeta.get("n_games", 0),
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "synced": now,
        "cutoff": cutoff,
        "trainedAt": mmeta.get("trained_at"),
        "games": int(n_train),
        "telemetryRows": int(telemetry_rows),
        "halflifeDays": mmeta.get("halflife_days", 60.0),
        "lanes": list(LANES),
        "traits": TRAITS,
        "archetypes": archetypes,
        "champions": champions,
        "pairs": {
            "ctr": quantize_centi_pp(v_ctr),
            "lane": quantize_centi_pp(v_lane),
            "syn": quantize_centi_pp(v_syn),
            "duo": quantize_centi_pp(v_duo),
            "jgl": quantize_centi_pp(v_jgl),
        },
    }


def store_snapshot(con, payload):
    synced = payload.get("synced") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = json.dumps(payload, separators=(",", ":"))
    with con:
        con.execute("DELETE FROM matchups_snapshot")
        con.execute("INSERT INTO matchups_snapshot VALUES (1, ?, ?)", (synced, text))


def api_matchups(con):
    """Return the latest matchups payload from lol.db, falling back to the
    committed lol/data/matchups/matchups.json archive."""
    row = next(iter(con.execute("SELECT payload FROM matchups_snapshot WHERE id = 1")), None)
    if row is not None:
        return json.loads(row[0])
    if os.path.exists(MATCHUPS_ARCHIVE):
        with open(MATCHUPS_ARCHIVE) as f:
            return json.load(f)
    return {"synced": None, "lanes": list(LANES), "traits": TRAITS,
            "archetypes": [], "champions": [], "pairs": {}}


def import_archive(con, data_dir):
    path = os.path.join(data_dir, "matchups", "matchups.json")
    if not os.path.exists(path):
        return False
    with open(path) as f:
        payload = json.load(f)
    store_snapshot(con, payload)
    return True


def _unpack_pair(vec, nc, antisym=False):
    out = [[0.0] * nc for _ in range(nc)]
    k = 0
    for i in range(nc):
        for j in range(i + 1, nc):
            val = vec[k] / 100.0
            out[i][j] = val
            out[j][i] = -val if antisym else val
            k += 1
    return out


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_sync(args):
    con = db_connect()
    payload = build_matchups_payload(
        args.quant_dir, con,
        shards_per_region=args.shards_per_region,
        platforms=args.platforms,
    )
    store_snapshot(con, payload)
    if not getattr(args, "db_only", False):
        os.makedirs(MATCHUPS_DATA_DIR, exist_ok=True)
        with open(MATCHUPS_ARCHIVE, "w") as f:
            json.dump(payload, f, separators=(",", ":"))
        size_kb = os.path.getsize(MATCHUPS_ARCHIVE) / 1024
        print(f"Synced matchups ({len(payload['champions'])} champions, "
              f"{len(payload['archetypes'])} archetypes, cutoff {payload['cutoff']}, "
              f"{payload['telemetryRows']:,} telemetry rows) -> lol.db + {MATCHUPS_ARCHIVE} ({size_kb:.0f} KB)")
    else:
        print(f"Synced matchups ({len(payload['champions'])} champions, "
              f"cutoff {payload['cutoff']}, {payload['telemetryRows']:,} telemetry rows) -> lol.db")


def cmd_archetypes(_args):
    data = api_matchups(db_connect())
    if not data["archetypes"]:
        sys.exit("No matchups data: run `cli.py matchups sync` or `cli.py import-json`.")
    champs = data["champions"]
    traits = data["traits"]
    for a in data["archetypes"]:
        sig = ", ".join(f"{traits[k]['short']} {z:+.1f}z" for k, z in a["sig"])
        names = ", ".join(champs[i]["name"] for i in a["members"][:10])
        more = f" (+{len(a['members']) - 10})" if len(a["members"]) > 10 else ""
        print(f"[{a['id']:2d}] {a['name']:<28} ({len(a['members']):2d})  {sig}")
        print(f"     {names}{more}")


def cmd_champion(args):
    data = api_matchups(db_connect())
    if not data["champions"]:
        sys.exit("No matchups data: run `cli.py matchups sync` or `cli.py import-json`.")
    champs = data["champions"]
    slug = _norm_slug(args.name)
    idx = next((i for i, c in enumerate(champs) if c["slug"] == slug), None)
    if idx is None:
        sys.exit(f"Unknown champion '{args.name}'.")
    ch = champs[idx]
    nc = len(champs)
    lane = args.lane or LANES[max(range(5), key=lambda r: ch["roles"][r])]
    r_idx = LANES.index(lane)

    ctr = _unpack_pair(data["pairs"]["ctr"], nc, antisym=True)
    ln = _unpack_pair(data["pairs"]["lane"], nc, antisym=True)
    syn = _unpack_pair(data["pairs"]["syn"], nc, antisym=False)
    duo = _unpack_pair(data["pairs"]["duo"], nc, antisym=False)
    jgl = _unpack_pair(data["pairs"]["jgl"], nc, antisym=False)

    arch = data["archetypes"][ch["arch"]]
    print(f"{ch['name']} ({lane}) | Archetype: {arch['name']} | Cutoff {data['cutoff']} ({data['games']:,} games)")
    print(f"Comp appetite: AP allies {ch['apSlope']:+.2f}pp | Frontline {ch['frontSlope']:+.2f}pp/z | Peel {ch['peelSlope']:+.2f}pp/z")

    role_tot = sum(c["roles"][r_idx] for c in champs) or 1
    opp = [j for j in range(nc) if j != idx and champs[j]["roles"][r_idx] / role_tot >= 0.005]
    opp.sort(key=lambda j: ctr[idx][j] + ln[idx][j])
    print(f"\nWorst {lane} lane matchups:")
    for j in opp[:6]:
        print(f"  {champs[j]['name']:<18} {ctr[idx][j] + ln[idx][j]:+6.2f}pp  ({data['archetypes'][champs[j]['arch']]['short']})")
    print(f"\nBest {lane} lane matchups:")
    for j in reversed(opp[-6:]):
        print(f"  {champs[j]['name']:<18} {ctr[idx][j] + ln[idx][j]:+6.2f}pp  ({data['archetypes'][champs[j]['arch']]['short']})")

    total_games = sum(sum(c["roles"]) for c in champs) or 1
    allies = [j for j in range(nc) if j != idx and sum(champs[j]["roles"]) / total_games >= 0.002]

    def kit_val(j):
        bonus = 0.0
        if r_idx in (3, 4):
            other_r = 4 if r_idx == 3 else 3
            if champs[j]["roles"][other_r] >= max(champs[j]["roles"]) * 0.4:
                bonus = duo[idx][j]
        elif r_idx == 1:
            if max(champs[j]["roles"][0], champs[j]["roles"][2]) >= max(champs[j]["roles"]) * 0.4:
                bonus = jgl[idx][j]
        elif r_idx in (0, 2):
            if champs[j]["roles"][1] >= max(champs[j]["roles"]) * 0.4:
                bonus = jgl[idx][j]
        return syn[idx][j] + bonus - ch["apSlope"] * champs[j]["dmgTilt"]

    allies.sort(key=kit_val, reverse=True)
    print("\nTop kit synergies (comp-damage adjusted):")
    for j in allies[:8]:
        print(f"  {champs[j]['name']:<18} {kit_val(j):+6.2f}pp  (raw {syn[idx][j]:+.2f}pp)")
