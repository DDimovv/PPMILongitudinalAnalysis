#!/usr/bin/env python3
import csv
import functools
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOK1 = "a975f089-3336-49f2-b954-000000000000"
TOK2 = "e3b73005-406d-430a-a90a-000000000000"


def run(script, *args):
    r = subprocess.run([sys.executable, "-I", os.path.join(HERE, script), *args], capture_output=True, text=True)
    out = r.stdout + r.stderr
    print(out)
    for secret in ("SECRET", TOK1[:8], TOK2[:8]):
        assert secret not in out, f"token {secret} leaked into output"
    return r.returncode, out


def main():
    root = tempfile.mkdtemp(prefix="ida_selftest_")
    files = os.path.join(root, "srv", "download", "files")
    dcm = b"\0" * 128 + b"DICM" + os.urandom(5000)
    os.makedirs(os.path.join(files, "ida1", TOK1))
    with zipfile.ZipFile(os.path.join(files, "ida1", TOK1, "coll.zip"), "w") as z:
        for iid, n in (("111", 3), ("222", 2), ("999", 1)):
            for k in range(n):
                z.writestr(f"PPMI/100001/SAG_3D_MPRAGE/2020-10-07_10_00_00.0/I{iid}/f{k}.dcm", dcm)
        z.writestr("PPMI_coll_metadata.xml", "<x/>")
    os.makedirs(os.path.join(files, "ida2", TOK2))
    with zipfile.ZipFile(os.path.join(files, "ida2", TOK2, "coll_dataset.zip"), "w") as z:
        z.writestr("README.txt", "You have attempted to download this file from IP address 1.2.3.4 ...")
    os.makedirs(os.path.join(files, "ida1", "slow"))
    shutil.copy(os.path.join(files, "ida1", TOK1, "coll.zip"), os.path.join(files, "ida1", "slow", "slow.zip"))

    class Handler(http.server.SimpleHTTPRequestHandler):
        slow_hits = 0

        def do_GET(self):
            if "/slow/" in self.path and Handler.slow_hits == 0:
                Handler.slow_hits += 1
                return self.send_error(503)
            return super().do_GET()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=os.path.join(root, "srv")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/download/files"
    urls = os.path.join(root, "urls.txt")
    with open(urls, "w") as f:
        f.write(f"{base}/ida1/{TOK1}/coll.zip?token=SECRET\n{base}/ida2/{TOK2}/coll_dataset.zip\n"
                f"{base}/ida1/gone/coll.zip?token=SECRET\n{base}/ida1/slow/slow.zip")

    dest, work = os.path.join(root, "data"), os.path.join(root, "work")
    man = os.path.join(root, "manifest.csv")
    with open(man, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["PATNO", "EVENT_ID", "image_id", "target_dir"])
        w.writerow(["100001", "BL", "111", os.path.join(dest, "100001", "BL_2020-10", "I111")])
        w.writerow(["100001", "BL", "222", os.path.join(dest, "100001", "BL_2020-10", "I222")])
        w.writerow(["100001", "V06", "333", os.path.join(dest, "100001", "V06_2022-11", "I333")])
    dl = ["--manifest", man, "--dest", dest, "--work", work, "--jobs", "2", "--retries", "1", "--not-ready-wait", "5"]

    rc, out = run("ida_download.py", "--urls", urls, *dl)
    assert rc == 2 and "'done': 2" in out and "'refused': 1" in out and "'expired': 1" in out, "first run summary"
    assert "not ready" in out, "503 not treated as not-ready"
    rc, out = run("ida_download.py", "--urls", urls, *dl)
    assert "'skipped': 2" in out, "finished zips not skipped on re-run"
    rc, out = run("check_downloads.py", "--manifest", man, "--dest", dest, "--out", work)
    assert rc == 1 and "missing: 1" in out and "ok: 2" in out, "check report"
    assert os.path.isfile(os.path.join(dest, "_unmapped", "I999", "f0.dcm")), "unmapped image not kept"

    man2 = os.path.join(root, "manifest2.csv")
    with open(man2, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["PATNO", "EVENT_ID", "image_id", "target_dir"])
        w.writerow(["100001", "BL", "111", os.path.join(dest, "100001", "BL_2020-10", "T1w", "I111")])
        w.writerow(["100001", "BL", "222", os.path.join(dest, "100001", "BL_2020-10", "T1w_extra", "I222")])
    rc, out = run("ida_download.py", "--urls", urls, urls, *dl[:1], man2, *dl[2:])
    assert "relocated 2" in out and "4 zip links" in out, "relocation / multiple url files"
    rc, out = run("check_downloads.py", "--manifest", man2, "--dest", dest, "--out", work)
    assert rc == 0, "check after relocation"

    for dp, _, fn in os.walk(work):
        for name in fn:
            blob = name + open(os.path.join(dp, name), "rb").read().decode("latin-1")
            assert "SECRET" not in blob and TOK1[:8] not in blob and TOK2[:8] not in blob, f"token in {name}"
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)
    print("SELFTEST OK")


if __name__ == "__main__":
    main()
