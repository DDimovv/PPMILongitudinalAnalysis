#!/usr/bin/env python3
import argparse
import csv
import os
import re
from collections import Counter, defaultdict
from datetime import datetime

T1_RE = re.compile(
    r"mprage|mp-rage|mp rage|spgr|bravo|tfe|3d[ _-]*t1|t1[ _-]*3d|t1w|t1[ _-]*weighted|"
    r"vt1|volumetric|^t1$|^t1\s*repeat$|sag.*t1",
    re.I,
)
EXCLUDE_RE = re.compile(
    r"phantom|locali[sz]er|scout|reformat|_mpr_(tra|cor)|snapshot|^ax\b|^coronal$|^transverse$|"
    r"\bt2\b|flair|dti|bold|rest",
    re.I,
)
DEPRIORITISE_RE = re.compile(r"_nd\b|\bnd$|repeat|rpt", re.I)
ND_RE = re.compile(r"_nd\b|\bnd$", re.I)


def protocol(s):
    return dict(kv.split("=", 1) for kv in s.split(";") if "=" in kv)


def is_3d_t1(row):
    desc = row["Description"].strip()
    if not T1_RE.search(desc) or EXCLUDE_RE.search(desc):
        return False
    p = protocol(row["Imaging Protocol"])
    if p.get("Acquisition Type") == "3D":
        return True
    try:
        return float(p.get("Slice Thickness", "99")) <= 1.5
    except ValueError:
        return False


def loni_visit_to_event(visit, planned_month_by_event):
    v = visit.strip()
    if v == "Baseline":
        return {"BL"}
    if v == "Symptomatic Therapy":
        return {"ST"}
    m = re.fullmatch(r"Unscheduled Visit (\d+)", v)
    if m:
        return {f"U{int(m.group(1)):02d}"}
    m = re.fullmatch(r"Month (\d+)", v)
    if m:
        return {e for e, pm in planned_month_by_event.items() if pm == m.group(1)}
    return set()


def parse_us_date(s):
    return datetime.strptime(s.strip(), "%m/%d/%Y").date()


def month_index(d):
    return d.year * 12 + d.month - 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True)
    ap.add_argument("--loni", required=True)
    ap.add_argument("--dest", required=True, help="image root on the cluster, e.g. ~/PPMILongitudinalAnalysis/data")
    ap.add_argument("--out", default=".")
    ap.add_argument("--date-window-months", type=int, default=2,
                    help="max |study month - INFODT month| for a match (default 2)")
    ap.add_argument("--primary-only", action="store_true",
                    help="keep one T1 series per time point instead of all of them")
    ap.add_argument("--patno", default="", help="comma-separated PATNOs to restrict to (dry runs)")
    ap.add_argument("--min-timepoints", type=int, default=1,
                    help="keep only participants with at least this many matched T1 time points")
    ap.add_argument("--drop-nd", action="store_true",
                    help="drop Siemens '_ND' (no distortion correction) copies when a corrected series exists")
    ap.add_argument("--batch-size", type=int, default=500, help="Image IDs per LONI collection batch")
    a = ap.parse_args()

    tps = list(csv.DictReader(open(a.list, newline="", encoding="utf-8-sig")))
    if a.patno:
        keep = {p.strip() for p in a.patno.split(",")}
        tps = [t for t in tps if t["PATNO"] in keep]
    planned = {}
    for t in tps:
        if t["planned_month"]:
            planned[t["EVENT_ID"]] = t["planned_month"]

    by_pat = defaultdict(list)
    n_loni = 0
    for r in csv.DictReader(open(a.loni, newline="", encoding="utf-8-sig")):
        n_loni += 1
        if r["Modality"] == "MRI" and is_3d_t1(r):
            r["_date"] = parse_us_date(r["Study Date"])
            by_pat[r["Subject ID"].strip()].append(r)

    manifest, unmatched, claimed, n_nd_dropped = [], [], set(), 0
    for t in tps:
        pat, ev = t["PATNO"], t["EVENT_ID"]
        mm, yyyy = t["INFODT"].split("/")
        info_mi = int(yyyy) * 12 + int(mm) - 1
        cands = by_pat.get(pat, [])
        if not cands:
            unmatched.append({**t, "reason": "no 3D T1 image for this PATNO in LONI"})
            continue
        within = [c for c in cands if abs(month_index(c["_date"]) - info_mi) <= a.date_window_months]
        labelled = [c for c in within if ev in loni_visit_to_event(c["Visit"], planned)]
        hits, how = (labelled, "visit+date") if labelled else (within, "date_only")
        if not hits:
            near = min(cands, key=lambda c: abs(month_index(c["_date"]) - info_mi))
            unmatched.append({**t, "reason": f"no 3D T1 within {a.date_window_months} months of INFODT; "
                                             f"nearest {near['Study Date']} ({near['Visit']})"})
            continue
        best_d = min({c["_date"] for c in hits}, key=lambda d: abs(month_index(d) - info_mi))
        hits = sorted((c for c in hits if c["_date"] == best_d),
                      key=lambda c: (bool(DEPRIORITISE_RE.search(c["Description"])), int(c["Image ID"])))
        if a.drop_nd and any(not ND_RE.search(c["Description"]) for c in hits):
            n_nd_dropped += sum(1 for c in hits if ND_RE.search(c["Description"]))
            hits = [c for c in hits if not ND_RE.search(c["Description"])]
        if a.primary_only:
            hits = hits[:1]
        folder = f"{ev}_{best_d:%Y-%m}"
        for i, c in enumerate(hits):
            iid = c["Image ID"].strip()
            if iid in claimed:
                continue
            claimed.add(iid)
            kind = "T1w" if i == 0 else "T1w_extra"
            manifest.append({
                "PATNO": pat, "EVENT_ID": ev, "INFODT": t["INFODT"], "study_date": f"{best_d:%Y-%m-%d}",
                "loni_visit": c["Visit"], "image_id": iid, "description": c["Description"],
                "research_group": c["Research Group"], "match": how, "is_primary": int(i == 0),
                "target_dir": f"{a.dest.rstrip('/')}/{pat}/{folder}/{kind}/I{iid}",
            })

    n_tp = defaultdict(set)
    for m in manifest:
        n_tp[m["PATNO"]].add(m["EVENT_ID"])
    too_few = {p for p, evs in n_tp.items() if len(evs) < a.min_timepoints}
    for t in tps:
        if t["PATNO"] in too_few and t["EVENT_ID"] in n_tp[t["PATNO"]]:
            unmatched.append({**t, "reason": f"excluded: participant has {len(n_tp[t['PATNO']])} matched T1 "
                                             f"time points (< --min-timepoints {a.min_timepoints})"})
    manifest = [m for m in manifest if m["PATNO"] not in too_few]
    n_tp = {p: evs for p, evs in n_tp.items() if p not in too_few}

    nb_n = {t["PATNO"]: t["n_timepoints"] for t in tps}
    parts = []
    for p in sorted(n_tp, key=int):
        rows = [m for m in manifest if m["PATNO"] == p and m["is_primary"]]
        dates = sorted(m["study_date"] for m in rows)
        d0, d1 = (datetime.strptime(dates[0], "%Y-%m-%d"), datetime.strptime(dates[-1], "%Y-%m-%d"))
        parts.append({
            "PATNO": p, "research_group": rows[0]["research_group"], "n_timepoints_notebook": nb_n.get(p, ""),
            "n_t1_timepoints": len(rows), "events": ";".join(m["EVENT_ID"] for m in sorted(rows, key=lambda m: m["study_date"])),
            "first_scan": dates[0], "last_scan": dates[-1],
            "followup_months": (d1.year - d0.year) * 12 + d1.month - d0.month,
            "n_extra_series": sum(1 for m in manifest if m["PATNO"] == p and not m["is_primary"]),
        })

    os.makedirs(a.out, exist_ok=True)
    mf = ["PATNO", "EVENT_ID", "INFODT", "study_date", "loni_visit", "image_id", "description",
          "research_group", "match", "is_primary", "target_dir"]
    with open(os.path.join(a.out, "manifest.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, mf); w.writeheader(); w.writerows(manifest)
    uf = list(tps[0].keys()) + ["reason"] if tps else ["reason"]
    with open(os.path.join(a.out, "unmatched.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, uf); w.writeheader(); w.writerows(unmatched)
    with open(os.path.join(a.out, "participants.csv"), "w", newline="") as f:
        pf = ["PATNO", "research_group", "n_timepoints_notebook", "n_t1_timepoints", "events",
              "first_scan", "last_scan", "followup_months", "n_extra_series"]
        w = csv.DictWriter(f, pf); w.writeheader(); w.writerows(parts)
    ids = [m["image_id"] for m in manifest]
    with open(os.path.join(a.out, "image_ids.txt"), "w") as f:
        f.write(",".join(ids) + "\n")
    for old in os.listdir(a.out):
        if re.fullmatch(r"image_ids_batch\d+\.txt", old):
            os.remove(os.path.join(a.out, old))
    n_batches = (len(ids) + a.batch_size - 1) // a.batch_size
    for b in range(n_batches):
        with open(os.path.join(a.out, f"image_ids_batch{b + 1:02d}.txt"), "w") as f:
            f.write(",".join(ids[b * a.batch_size:(b + 1) * a.batch_size]) + "\n")

    tp_hit = {(m["PATNO"], m["EVENT_ID"]) for m in manifest}
    print(f"LONI rows read: {n_loni}; 3D T1 rows kept: {sum(map(len, by_pat.values()))}")
    print(f"time points in list: {len(tps)}  kept: {len(tp_hit)}  unmatched/excluded: {len(unmatched)}")
    print(f"participants in list: {len({t['PATNO'] for t in tps})}  kept: {len(parts)} "
          f"(min {a.min_timepoints} T1 time points; excluded for too few: {len(too_few)})")
    print("kept participants by research group:", dict(Counter(p["research_group"] for p in parts).most_common()))
    print(f"images in manifest: {len(manifest)} (primary: {sum(m['is_primary'] for m in manifest)}, "
          f"extra: {sum(1 for m in manifest if not m['is_primary'])}; ND copies dropped: {n_nd_dropped})")
    print(f"LONI batches of {a.batch_size}: {n_batches} (image_ids_batchNN.txt)")
    print("match types:", {k: sum(1 for m in manifest if m['match'] == k and m['is_primary']) for k in ('visit+date', 'date_only')})
    reasons = defaultdict(int)
    for u in unmatched:
        reasons[u["reason"].split(";")[0]] += 1
    print("unmatched reasons:", dict(reasons))


if __name__ == "__main__":
    main()
