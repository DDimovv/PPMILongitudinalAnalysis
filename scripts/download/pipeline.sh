#!/bin/bash
set -euo pipefail

PROJECT=${PROJECT:-$HOME/PPMILongitudinalAnalysis}
SIF=${SIF:-containers/py312.sif}
PARTITION=${PARTITION:-openlab-queue}
DEST=${DEST:-data}
WORK=${WORK:-work}
LIST=${LIST:-inputs/mri_3_4_timepoints_download_list.csv}
LONI=${LONI:-$(ls -t "$PROJECT"/inputs/loni_mri_search_*.csv 2>/dev/null | head -1)}
HERE=scripts/download

usage() { echo "usage: $0 {manifest|submit|status|check} NAME [build_manifest.py options]"; exit 1; }

cd "$PROJECT"
cmd=${1:-}; name=${2:-}
[ -n "$cmd" ] && [ -n "$name" ] || usage
shift 2
run() {
  srun --partition="$PARTITION" --ntasks=1 --cpus-per-task=1 --mem=2G --time=00:30:00 \
       --job-name="ppmi_$cmd" singularity exec "$SIF" python3 "$@" < /dev/null
}

case "$cmd" in
  manifest)
    [ -f "$SIF" ] || { echo "missing $SIF: singularity build $SIF docker://python:3.12-slim"; exit 1; }
    [ -n "$LONI" ] || { echo "no inputs/loni_mri_search_*.csv"; exit 1; }
    run "$HERE/build_manifest.py" --list "$LIST" --loni "$LONI" \
        --dest "$PROJECT/$DEST" --out "manifests/$name" "$@"
    echo "Next: one LONI collection per manifests/$name/image_ids_batchNN.txt;"
    echo "      save each URL List as urls/${name}_batchNN.txt, then: $0 submit $name"
    ;;
  submit)
    m=manifests/$name/manifest.csv
    [ -f "$m" ] || { echo "no $m; run: $0 manifest $name ..."; exit 1; }
    shopt -s nullglob; files=(urls/"${name}"_*.txt); shopt -u nullglob
    [ ${#files[@]} -gt 0 ] || { echo "no urls/${name}_*.txt link files yet"; exit 1; }
    chmod 600 urls/* 2>/dev/null || true
    n_links=$(grep -ho 'https://[^[:space:],"]*\.zip' "${files[@]}" | wc -l)
    n_batches=$(ls manifests/"$name"/image_ids_batch*.txt 2>/dev/null | wc -l)
    echo "${#files[@]} link files (${n_links} zip links) for $n_batches image batches"
    [ "${#files[@]}" -ge "$n_batches" ] || echo "WARNING: fewer link files than batches; missing batches are not downloaded"
    mkdir -p "$DEST" logs
    cp "manifests/$name/participants.csv" "$DEST/participants.csv"
    cp "$m" "$DEST/manifest.csv"
    sbatch --partition="$PARTITION" \
      --export=ALL,URLS="${files[*]}",MANIFEST="$m",DEST="$DEST",WORK="$WORK",SIF="$SIF" \
      "$HERE/download_job.sh"
    ;;
  status)
    squeue -u "$USER" -o "%.10i %.12j %.8T %.10M %.12l %R" | grep -E "JOBID|ppmi" || true
    log=$(ls -t logs/download_*.out 2>/dev/null | head -1 || true)
    [ -n "$log" ] && { echo "== $log (last lines)"; tail -n 8 "$log"; }
    done_zips=$(ls "$WORK"/state/*.done 2>/dev/null | wc -l)
    total=$(grep -ho 'https://[^[:space:],"]*\.zip' urls/"${name}"_*.txt 2>/dev/null | wc -l)
    imgs=$(find "$DEST" -mindepth 4 -maxdepth 4 -type d -name 'I*' 2>/dev/null | wc -l)
    want=$(($(wc -l < "manifests/$name/manifest.csv") - 1))
    echo "zips done: $done_zips / $total   image folders: $imgs / $want"
    du -sh "$DEST" "$WORK/zips" 2>/dev/null || true
    ;;
  check)
    run "$HERE/check_downloads.py" --manifest "manifests/$name/manifest.csv" --dest "$DEST" --out "$WORK"
    ;;
  *) usage ;;
esac
