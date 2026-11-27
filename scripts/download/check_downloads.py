#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import os
import sys


def is_dicom(path):
    with open(path, "rb") as f:
        head = f.read(132)
    return len(head) == 132 and head[128:132] == b"DICM"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--rehash", action="store_true")
    a = ap.parse_args()
    dest = os.path.abspath(os.path.expanduser(a.dest))
    out = os.path.abspath(os.path.expanduser(a.out or os.path.join(os.path.dirname(dest), "work")))
    os.makedirs(out, exist_ok=True)

    source = {}
    state = os.path.join(out, "state")
    if os.path.isdir(state):
        for fn in os.listdir(state):
            if fn.endswith(".done"):
                rec = json.load(open(os.path.join(state, fn)))
                for iid in rec.get("image_ids", []):
                    source[iid] = (rec["zip"], rec.get("url", ""))

    cache_path = os.path.join(out, ".checksum_cache.json")
    cache = {} if a.rehash or not os.path.exists(cache_path) else json.load(open(cache_path))
    rows, sums, counts = [], [], {}
    for r in csv.DictReader(open(a.manifest, newline="")):
        tgt = os.path.expanduser(r["target_dir"])
        problems, files = [], []
        if not os.path.isdir(tgt):
            status = "missing"
        else:
            files = sorted(f for f in os.listdir(tgt) if os.path.isfile(os.path.join(tgt, f)))
            if not files:
                status = "empty"
            else:
                for f in files:
                    p = os.path.join(tgt, f)
                    st = os.stat(p)
                    if st.st_size == 0:
                        problems.append(f"{f}:zero-bytes")
                        continue
                    if f.lower().endswith(".dcm") and not is_dicom(p):
                        problems.append(f"{f}:not-dicom")
                    key = f"{st.st_size}:{int(st.st_mtime)}"
                    c = cache.get(p)
                    if not c or c[0] != key:
                        c = cache[p] = [key, sha256(p)]
                    sums.append(f"{c[1]}  {p}")
                status = "bad" if problems else "ok"
        counts[status] = counts.get(status, 0) + 1
        rows.append({"PATNO": r["PATNO"], "EVENT_ID": r["EVENT_ID"], "image_id": r["image_id"],
                     "status": status, "n_files": len(files), "problems": ";".join(problems[:20]),
                     "source_zip": source.get(r["image_id"], ("", ""))[0],
                     "source_url": source.get(r["image_id"], ("", ""))[1],
                     "target_dir": tgt})

    with open(os.path.join(out, "check_report.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, list(rows[0].keys()) if rows else ["status"])
        w.writeheader(); w.writerows(rows)
    with open(os.path.join(out, "checksums.sha256"), "w") as f:
        f.write("\n".join(sums) + ("\n" if sums else ""))
    with open(cache_path, "w") as f:
        json.dump(cache, f)

    print(f"images in manifest: {len(rows)}  " + "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    print(f"files checksummed: {len(sums)}")
    for r in rows:
        if r["status"] != "ok":
            print(f"  {r['status']:7s} {r['PATNO']} {r['EVENT_ID']} I{r['image_id']} {r['problems']}")
    print(f"report: {os.path.join(out, 'check_report.csv')}\nchecksums: {os.path.join(out, 'checksums.sha256')}")
    sys.exit(0 if counts.get("ok", 0) == len(rows) else 1)


if __name__ == "__main__":
    main()
