#!/bin/bash
# Put the end of each log on the run's summary page as an error annotation.
#
#   scripts/ci-annotate.sh <title> <lines> <file>...
#
# Annotations are readable through the public checks API without signing in,
# unlike job logs, so a failing step says why to anyone looking at the run.
# One annotation per file, because GitHub cuts each one at 4 KB: a long first
# file must not hide the next. Each keeps its file's last <lines> lines, then
# its last 4000 bytes; a missing or empty file is skipped.
set -u
title=$1 lines=$2
shift 2
for f in "$@"; do
  [ -s "$f" ] || continue
  body="$(tail -n "$lines" "$f" | tail -c 4000)"
  # Workflow commands end at a newline: encode it, and % and CR before it.
  body=${body//'%'/'%25'}
  body=${body//$'\r'/'%0D'}
  body=${body//$'\n'/'%0A'}
  echo "::error title=${title}: ${f}::${body}"
done
