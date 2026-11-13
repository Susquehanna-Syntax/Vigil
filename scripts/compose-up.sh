#!/bin/bash
# Start the compose stack in CI, saying why when it cannot.
#
#   scripts/compose-up.sh
#
# The third-party images (database, redis, guacd) come from Docker Hub, whose
# anonymous pulls fail now and then; a failed pull ends `docker compose up`
# before any container starts. So pull those first, retrying, and leave the
# Vigil image alone: CI built it locally under the tag the compose file names,
# and a pull would replace it with the published one. compose-up.log keeps
# what Compose said, for scripts/ci-annotate.sh.
set -u
: > compose-up.log
for image in $(docker compose config --images | sort -u | grep -v '^susquehannasyntax/vigil:'); do
  for attempt in 1 2 3 4; do
    docker pull -q "$image" >> compose-up.log 2>&1 && break
    [ "$attempt" = 4 ] && { echo "could not pull $image" | tee -a compose-up.log; exit 1; }
    sleep $((attempt * 15))
  done
done
for attempt in 1 2; do
  docker compose up -d >> compose-up.log 2>&1 && exit 0
  echo "docker compose up failed (attempt $attempt)" | tee -a compose-up.log
  sleep 10
done
tail -20 compose-up.log
exit 1
