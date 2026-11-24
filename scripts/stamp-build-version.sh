#!/usr/bin/env bash
# Stamp the full version of a tag build into the server and the agent.
#
# A tag vX.Y.Z-rcN or vX.Y.Z-exN names a build the code alone cannot: the
# source says 2026.17.0 for every candidate. This writes the tag's version to
# server/vigil/BUILD_VERSION (settings.py reads it into VIGIL_VERSION) and into
# the agent's __version__, so About, the agent list and the outdated-agent
# check all say 2026.17.0-rc1. A branch push or a plain vX.Y.Z tag changes
# nothing. Run from the repository root; GITHUB_REF_TYPE / GITHUB_REF_NAME
# come from Actions.
set -euo pipefail
cd "$(dirname "$0")/.."
[ "${GITHUB_REF_TYPE:-}" = "tag" ] || { echo "not a tag build: nothing to stamp"; exit 0; }
tag="${GITHUB_REF_NAME:?}"
version="${tag#v}"
case "$version" in
  *-rc[0-9]*|*-ex[0-9]*) ;;
  *) echo "release tag $tag: the source version stands"; exit 0 ;;
esac
base="$(sed -n 's/^VIGIL_VERSION = "\(.*\)"$/\1/p' server/vigil/settings.py)"
[ "${version%%-*}" = "$base" ] || { echo "::error::$tag does not match VIGIL_VERSION $base"; exit 1; }
printf '%s\n' "$version" > server/vigil/BUILD_VERSION
printf '__version__ = "%s"\n' "$version" > agent/vigil_agent/__version__.py
echo "stamped $version"
