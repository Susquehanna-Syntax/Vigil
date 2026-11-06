#!/bin/sh
# Wait until every running service in the compose project with a healthcheck
# is healthy and the one-shot `secrets` service exited 0. Fails after
# <seconds> (default 300), or at once if a container exits non-zero.
#
#   scripts/compose-wait-healthy.sh [seconds]
#
# `docker compose up --wait` treats a one-shot service that has finished as a
# failure on some Compose versions, so this checks each container itself.
set -eu
limit="${1:-300}"
start=$(date +%s)
while :; do
  pending=0
  for id in $(docker compose ps -aq); do
    name=$(docker inspect -f '{{.Name}}' "$id")
    state=$(docker inspect -f '{{.State.Status}}' "$id")
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$id")
    code=$(docker inspect -f '{{.State.ExitCode}}' "$id")
    if [ "$state" = exited ]; then
      [ "$code" = 0 ] || { echo "$name exited with $code"; docker logs --tail 50 "$id"; exit 1; }
    elif [ -n "$health" ] && [ "$health" != healthy ]; then
      pending=1
    elif [ "$state" != running ]; then
      pending=1
    fi
  done
  [ "$pending" = 0 ] && { echo "every service is up"; exit 0; }
  [ $(( $(date +%s) - start )) -ge "$limit" ] && { echo "timed out"; docker compose ps -a; exit 1; }
  sleep 3
done
