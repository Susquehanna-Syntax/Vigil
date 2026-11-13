#!/bin/bash
# Put the end of a log on the run's summary page as one error annotation.
#
#   scripts/ci-annotate.sh <title> <lines> <file>...
#
# Annotations are readable through the public checks API without signing in,
# unlike job logs, so a failing step says why to anyone looking at the run.
# Each file contributes its last <lines> lines; a missing file is skipped.
set -u
title=$1 lines=$2
shift 2
body=""
for f in "$@"; do
  [ -s "$f" ] || continue
  body+="== ${f}"$'\n'"$(tail -n "$lines" "$f")"$'\n'
done
[ -n "$body" ] || exit 0
# Workflow commands end at a newline: encode it, and % and CR before it.
body=${body//'%'/'%25'}
body=${body//$'\r'/'%0D'}
body=${body//$'\n'/'%0A'}
echo "::error title=${title}::${body}"
