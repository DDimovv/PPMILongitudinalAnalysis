#!/bin/bash
#SBATCH --job-name ppmi_mri_dl
#SBATCH --output logs/download_%j.out
#SBATCH --partition=openlab-queue
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=4G
#SBATCH --time=1-00:00:00
set -euo pipefail

: "${URLS:?set URLS=<file with IDA zip links>}"
: "${MANIFEST:?set MANIFEST=<manifest.csv>}"
DEST=${DEST:-data}
WORK=${WORK:-work}
JOBS=${JOBS:-3}
SIF=${SIF:-containers/py312.sif}
COOKIES_ARG=()
[ -n "${COOKIES:-}" ] && COOKIES_ARG=(--cookies "$COOKIES")

env | grep -E '^SLURM_(JOB_ID|JOB_NODELIST|JOB_PARTITION)=' || true
echo "URLS=$URLS MANIFEST=$MANIFEST DEST=$DEST WORK=$WORK JOBS=$JOBS"

rc=0
singularity exec "$SIF" python3 scripts/download/ida_download.py \
    --urls $URLS --manifest "$MANIFEST" --dest "$DEST" --work "$WORK" --jobs "$JOBS" "${COOKIES_ARG[@]}" || rc=$?
echo "downloader exit code: $rc"

singularity exec "$SIF" python3 scripts/download/check_downloads.py \
    --manifest "$MANIFEST" --dest "$DEST" --out "$WORK" || echo "check reported missing/bad images (see above)"
exit $rc
