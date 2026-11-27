# PPMI MRI download (LONI IDA → FINKI GPGPU cluster)

Downloads the 3D T1 MRI series for the participants selected in
`PPMI_MRI_Timepoint_Distribution_and_Data_Sources.ipynb` **directly onto the cluster**. Images
never pass through a laptop, and the LONI password is never typed or stored: downloads use zip
links that you generate in IDA yourself.

| File | Runs on | Does |
|---|---|---|
| `pipeline.sh` | cluster | driver: `manifest`, `submit`, `status`, `check` (all compute goes through Slurm) |
| `build_manifest.py` | cluster (or laptop) | maps PATNO + visit to LONI Image IDs (3D T1); writes manifest, participants, Image-ID batches |
| `ida_download.py` | cluster, in container | downloads IDA zips in parallel (resume, retry), tests them, sorts images into folders |
| `check_downloads.py` | cluster, in container | lists missing/empty/non-DICOM images; writes `check_report.csv`, `checksums.sha256` |
| `download_job.sh` | cluster, `sbatch` | Slurm job (CPU only, `openlab-queue`, 1-day limit): download, then check |
| `loni_tunnel.ps1` | Windows laptop | SSH tunnel + Chrome proxy rule so LONI links are bound to the cluster's IP |
| `selftest.py` | anywhere | offline end-to-end test against a fake LONI server (`python3 selftest.py`) |

All Python is standard library only (container: `python:3.12-slim`).

## Folder layout on the cluster (`~/PPMILongitudinalAnalysis`)

```
data/participants.csv                       one row per participant: research group, T1 visits, follow-up
data/manifest.csv                           one row per image: visit, date, LONI Image ID, description, path
data/<PATNO>/<EVENT_ID>_<YYYY-MM>/T1w/I<image_id>/*.dcm        preferred 3D T1 of the visit
data/<PATNO>/<EVENT_ID>_<YYYY-MM>/T1w_extra/I<image_id>/*.dcm  other T1 series (repeats, alternates)
data/_metadata/   LONI XML metadata         data/_unmapped/   images in a zip but not in the manifest
containers/py312.sif   inputs/   manifests/<name>/   urls/ (links, chmod 600, never committed)
work/zips/   work/state/*.done   work/downloads.csv   work/check_report.csv   work/checksums.sha256
logs/download_<jobid>.out
```

`EVENT_ID` is the PPMI visit code (BL, V04 = month 12, V06 = 24, V10 = 48, ...); `YYYY-MM` is the
month of the scan. Most series are 192 single-slice DICOMs; some newer scans are one enhanced
multi-frame DICOM.

## One-time setup

```bash
# laptop: EDUVPN on, then
ssh <user>@10.10.65.6
mkdir -p ~/PPMILongitudinalAnalysis/{containers,inputs,manifests,urls,logs} && cd ~/PPMILongitudinalAnalysis
chmod 700 urls
singularity build containers/py312.sif docker://python:3.12-slim   # Docker is not usable by normal users
# copy scripts/download/ from this repo to scripts/download/ (scp or git clone)
# copy mri_3_4_timepoints_download_list.csv (notebook section 8) and the LONI search CSV to inputs/
```

LONI search CSV: IDA → Search → Advanced Image Search, Modality = MRI, tick every "Display in
result" box, SEARCH, CSV Download. Save it as `inputs/loni_mri_search_<date>.csv`; the newest is used.

## 1. Manifest

```bash
scripts/download/pipeline.sh manifest t1_min3 --min-timepoints 3 --drop-nd
```

Options: `--min-timepoints N` (participants with at least N matched T1 visits), `--drop-nd` (skip
Siemens `_ND` copies, the same scan without on-scanner distortion correction, unless it is the only
one), `--primary-only` (one T1 per visit), `--patno 100001,100012` (subset), `--batch-size 500`.

Matching: a series counts as 3D T1 when its description looks like MPRAGE/SPGR/3D T1 (phantoms,
localizers, reformats excluded) and it is tagged 3D or has slices ≤ 1.5 mm. LONI's own 2D/3D and
weighting tags are often wrong, so they are not used alone. A visit matches when the LONI visit
label agrees with `EVENT_ID` and the scan is within 2 months of `INFODT`. `manifests/<name>/unmatched.csv`
lists every visit that is not downloaded and why (mostly: LONI has no images yet for 2024–2026 visits).

## 2. Links (you, in the browser)

**LONI binds every link to the IP that created it.** The cluster downloads from its own egress IP,
so links made in a normal browser at home are `REFUSED` (the zip holds only a README naming both
IPs). Make them through the cluster:

```powershell
# laptop, EDUVPN on, all Chrome windows closed:
.\scripts\download\loni_tunnel.ps1 -User <cluster-username>
```

It prints the IP LONI will see (must be the cluster's) and opens IDA. Log in yourself (the IP
change ends the old session). Then, for each `manifests/<name>/image_ids_batchNN.txt`:

1. Search → Advanced Image Search → paste the batch into **Image ID** → SEARCH → Select All →
   Add To Collection → new collection `<name>_batchNN`.
2. Data Collections → the collection → tick All → **ADVANCED DOWNLOAD** (never 1-Click, which
   downloads to the laptop) → **URL List**.
3. Put the list on the cluster as `urls/<name>_batchNN.txt` (paste with `nano`, or `scp` the file).

Links expire after **10 days** or when the download completes. Logs show them with the token
masked (`…/ida1/<token>/…zip`).

## 3. Download

```bash
scripts/download/pipeline.sh submit t1_min3     # one Slurm job for all urls/t1_min3_*.txt
scripts/download/pipeline.sh status t1_min3     # job state, zips done, image folders, disk use
```

Defaults: 3 parallel downloads, 6 retries with back-off; HTTP 503 (LONI still building a zip right
after the links were made) is waited out for up to an hour without using retries. Override with
`JOBS=`, `DEST=`, `WORK=`, `PARTITION=` in the environment. The cluster runs **one job per user at a
time** (`AssocMaxJobsLimit`), so `manifest` and `check` wait in the queue while a download runs. Measured: about 14 MB of zip and 38 MB on disk per series,
about 6–7 MB/s from `gpgpu01`.

## Resume / re-run

* **Re-running is always safe.** Finished zips are skipped (`work/state/*.done`), interrupted zips
  resume from `work/zips/*.part`, images already in place are not rewritten, and image folders from
  an older layout are moved (not copied) to their manifest path.
* **Job hit the 1-day limit, was cancelled, or LONI was down:** `pipeline.sh submit <name>` again.
* **`EXPIRED`** (HTTP 401/403/404/410 or login page): make a new URL List for that collection (step 2),
  overwrite `urls/<name>_batchNN.txt`, submit again.
* **`REFUSED`:** links were made from a different IP; redo step 2 through `loni_tunnel.ps1`.
* **`CORRUPT`:** the zip was renamed to `*.zip.bad` and is fetched again on the next run.
* **Check only:** `pipeline.sh check <name>`; exit code 0 only when every image is ok.
  `missing` rows in `work/check_report.csv` are the Image IDs to put in a new collection.
* **More images later** (LONI catches up on recent visits): save a new search CSV in `inputs/`,
  re-run `manifest` under a new name, make links for it, `submit`. Existing images are kept.
* **Verify files later:** `sha256sum -c --quiet work/checksums.sha256`

Nothing in these scripts deletes downloaded data. Remove `work/zips/` yourself once the check
reports everything ok.
