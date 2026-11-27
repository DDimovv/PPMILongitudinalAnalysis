#!/usr/bin/env python3
import argparse
import concurrent.futures as cf
import csv
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

CHUNK = 1 << 20
EXTRACT_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()


def log(msg):
    with LOG_LOCK:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


TOKEN_SEG_RE = re.compile(r"^[0-9a-fA-F-]{16,}$")


def safe_url(u):
    p = urllib.parse.urlsplit(u)
    path = "/".join("<token>" if TOKEN_SEG_RE.match(s) else s for s in p.path.split("/"))
    return f"{p.scheme}://{p.netloc}{path}"


def read_urls(path):
    urls = []
    for line in open(path, encoding="utf-8-sig"):
        for u in re.findall(r"https?://\S+", line):
            u = u.strip("'\",;")
            if urllib.parse.urlsplit(u).path.lower().endswith(".zip"):
                urls.append(u)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def zip_name(u):
    path = urllib.parse.urlsplit(u).path
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    return f"{stem}_{hashlib.sha1(path.encode()).hexdigest()[:10]}{ext}"


class RefusedError(Exception):
    pass


def check_zip(zpath):
    with zipfile.ZipFile(zpath) as z:
        bad = z.testzip()
        if bad:
            raise zipfile.BadZipFile(f"CRC error in {bad}")
        names = [n for n in z.namelist() if not n.endswith("/")]
        if not any(IMG_RE.search(n) for n in names):
            msg = ""
            if "README.txt" in names:
                msg = z.read("README.txt").decode("utf-8", "replace").strip()
            raise RefusedError(msg or f"zip has no image folders ({len(names)} files)")


class AuthError(Exception):
    pass


class NotReady(Exception):
    pass


def make_opener(cookies):
    handlers = []
    if cookies:
        cj = http.cookiejar.MozillaCookieJar(cookies)
        cj.load(ignore_discard=True, ignore_expires=True)
        handlers.append(urllib.request.HTTPCookieProcessor(cj))
    op = urllib.request.build_opener(*handlers)
    op.addheaders = [("User-Agent", "ppmi-mri-downloader/1.0 (python-urllib)")]
    return op


def fetch(opener, url, part, timeout):
    have = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", f"bytes={have}-")
    try:
        r = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:
            return
        if e.code in (401, 403, 404, 410):
            raise AuthError(f"HTTP {e.code} (link expired or not authorised)")
        if e.code == 503:
            raise NotReady("HTTP 503 (zip still being prepared by LONI)")
        raise
    if "login" in r.geturl() or r.headers.get_content_type() == "text/html":
        raise AuthError("redirected to an HTML/login page (link expired or session needed)")
    mode = "ab" if (have and r.status == 206) else "wb"
    with r, open(part, mode) as f:
        while True:
            b = r.read(CHUNK)
            if not b:
                break
            f.write(b)
    total = r.headers.get("Content-Range", "").split("/")[-1] if r.status == 206 else r.headers.get("Content-Length")
    if total and total.isdigit() and os.path.getsize(part) != int(total):
        raise IOError(f"short read: {os.path.getsize(part)} of {total} bytes")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


IMG_RE = re.compile(r"(?:^|/)I(\d+)/([^/]+)$")


def extract(zpath, targets, dest):
    images, files, unmapped = set(), 0, set()
    with zipfile.ZipFile(zpath) as z:
        groups = {}
        for m in z.infolist():
            if m.is_dir():
                continue
            mt = IMG_RE.search(m.filename)
            if not mt:
                out = os.path.join(dest, "_metadata", m.filename)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                if not os.path.exists(out):
                    with z.open(m) as s, open(out, "wb") as d:
                        shutil.copyfileobj(s, d)
                continue
            groups.setdefault(mt.group(1), []).append((m, mt.group(2)))
        for iid, members in groups.items():
            tgt = targets.get(iid)
            if tgt is None:
                unmapped.add(iid)
                tgt = os.path.join(dest, "_unmapped", f"I{iid}")
            if os.path.isdir(tgt) and len(os.listdir(tgt)) >= len(members):
                images.add(iid)
                continue
            tmp = tgt + ".partial"
            if os.path.isdir(tmp):
                shutil.rmtree(tmp)
            os.makedirs(tmp)
            for m, base in members:
                with z.open(m) as s, open(os.path.join(tmp, base), "wb") as d:
                    shutil.copyfileobj(s, d)
                files += 1
            if os.path.isdir(tgt):
                for f in os.listdir(tmp):
                    if not os.path.exists(os.path.join(tgt, f)):
                        os.replace(os.path.join(tmp, f), os.path.join(tgt, f))
                shutil.rmtree(tmp)
            else:
                os.makedirs(os.path.dirname(tgt), exist_ok=True)
                os.replace(tmp, tgt)
            images.add(iid)
    return sorted(images), files, sorted(unmapped)


def relocate(targets, dest):
    moved = 0
    for root, dirs, _ in os.walk(dest):
        depth = os.path.relpath(root, dest).count(os.sep)
        if os.path.basename(root).startswith("_") or depth >= 4:
            dirs[:] = []
            continue
        for d in list(dirs):
            m = re.fullmatch(r"I(\d+)", d)
            if not m or m.group(1) not in targets:
                continue
            src, tgt = os.path.join(root, d), targets[m.group(1)]
            if os.path.abspath(src) != os.path.abspath(tgt) and not os.path.exists(tgt):
                os.makedirs(os.path.dirname(tgt), exist_ok=True)
                os.replace(src, tgt)
                dirs.remove(d)
                moved += 1
    if moved:
        log(f"relocated {moved} existing image folders to the manifest layout")


def process(url, a, opener, targets):
    name = zip_name(url)
    done = os.path.join(a.work, "state", name + ".done")
    if os.path.exists(done):
        log(f"skip (done)  {name}")
        return {"zip": name, "status": "skipped"}
    zpath = os.path.join(a.work, "zips", name)
    part = zpath + ".part"
    if not os.path.exists(zpath):
        attempt, waited_not_ready = 0, 0
        while True:
            attempt += 1
            try:
                log(f"download     {name}  attempt {attempt}  {safe_url(url)}")
                fetch(opener, url, part, a.timeout)
                os.replace(part, zpath)
                break
            except AuthError as e:
                log(f"EXPIRED      {name}: {e}")
                return {"zip": name, "status": "expired", "error": str(e)}
            except NotReady as e:
                if waited_not_ready >= a.not_ready_wait:
                    return {"zip": name, "status": "failed", "error": f"not ready after {waited_not_ready}s"}
                attempt -= 1
                step = max(1, min(120, a.not_ready_wait))
                log(f"not ready    {name}: {e}; waiting {step}s")
                time.sleep(step)
                waited_not_ready += step
            except Exception as e:
                if attempt >= a.retries:
                    return {"zip": name, "status": "failed", "error": f"retries exhausted: {e!r}"}
                wait = min(300, 15 * 2 ** (attempt - 1))
                log(f"retry        {name}: {e!r}; waiting {wait}s")
                time.sleep(wait)
    try:
        check_zip(zpath)
    except (zipfile.BadZipFile, RefusedError) as e:
        status = "refused" if isinstance(e, RefusedError) else "corrupt"
        bad_path = zpath + ".bad"
        os.replace(zpath, bad_path)
        log(f"{status.upper():12s} {name}: {e}; moved to {os.path.basename(bad_path)}")
        return {"zip": name, "status": status, "error": str(e)[:300]}
    digest, size = sha256(zpath), os.path.getsize(zpath)
    with EXTRACT_LOCK:
        image_ids, n_files, unmapped = extract(zpath, targets, a.dest)
    n_img = len(image_ids)
    rec = {"zip": name, "status": "done", "bytes": size, "sha256": digest, "images": n_img,
           "image_ids": image_ids, "files_written": n_files, "unmapped": unmapped, "url": safe_url(url),
           "finished": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(done, "w") as f:
        json.dump(rec, f)
    log(f"done         {name}  {size/1e6:.1f} MB  {n_img} images  unmapped={len(unmapped)}")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", required=True, nargs="+", help="one or more files with zip links")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--work", default=None)
    ap.add_argument("--jobs", type=int, default=3, help="parallel downloads (default 3)")
    ap.add_argument("--retries", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=120, help="socket timeout in seconds")
    ap.add_argument("--not-ready-wait", type=int, default=3600,
                    help="seconds to keep waiting on HTTP 503 while LONI builds a zip (default 3600)")
    ap.add_argument("--cookies", default=None, help="optional Netscape cookies.txt you exported yourself")
    a = ap.parse_args()
    a.dest = os.path.abspath(os.path.expanduser(a.dest))
    a.work = os.path.abspath(os.path.expanduser(a.work or os.path.join(os.path.dirname(a.dest), "work")))
    for d in (a.dest, os.path.join(a.work, "zips"), os.path.join(a.work, "state")):
        os.makedirs(d, exist_ok=True)

    targets = {}
    for r in csv.DictReader(open(a.manifest, newline="")):
        targets[r["image_id"]] = os.path.expanduser(r["target_dir"])
    relocate(targets, a.dest)
    urls = [u for f in a.urls for u in read_urls(f)]
    urls = list(dict.fromkeys(urls))
    log(f"{len(urls)} zip links, {len(targets)} manifest images, dest={a.dest}, work={a.work}, jobs={a.jobs}")
    if not urls:
        sys.exit("no .zip links found in --urls file")

    opener = make_opener(a.cookies)
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        results = list(ex.map(lambda u: process(u, a, opener, targets), urls))

    log_path = os.path.join(a.work, "downloads.csv")
    new = not os.path.exists(log_path)
    with open(log_path, "a", newline="") as f:
        w = csv.DictWriter(f, ["finished", "zip", "status", "bytes", "sha256", "images", "files_written",
                               "url", "error"], extrasaction="ignore")
        if new:
            w.writeheader()
        for r in results:
            if r["status"] != "skipped":
                w.writerow({**r, "finished": r.get("finished", time.strftime("%Y-%m-%dT%H:%M:%S"))})
    summary = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    log(f"summary {summary}")
    if any(r["status"] in ("expired", "failed", "corrupt", "refused") for r in results):
        sys.exit(2)


if __name__ == "__main__":
    main()
