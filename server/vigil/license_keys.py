"""Which public key licenses must be signed with.

An image built from the Dockerfile carries a release marker and trusts only
the keys written here: VIGIL_LICENSE_PUBLIC_KEY is ignored, so nobody can
sign themselves a license and point Vigil at their own key with an
environment variable. Getting round that takes building a modified image,
which is as much work as patching the gate out of the source; that is the
honest limit for AGPL software, and it is where this stops.

A source checkout has no marker and keeps the development behaviour: the
variable if set, the dev key when DEBUG.
"""
from pathlib import Path

#: Mercantil's production key. Born in KMS the day the license server goes
#: live (Mercantil docs/architecture.md); until then release builds verify no
#: license at all, which is the free tier and nothing worse.
PRODUCTION_PUBLIC_KEY = ""
#: Mercantil's dev key. Its private half never leaves the developers' machines
#: (gitignored there), so trusting it lets nobody else sign anything.
DEV_PUBLIC_KEY = "HDIgm72yEkIopWgvsv0Q6Gp695l4ecOZMYnP2by7+IQ="
#: Written into the image by the Dockerfile; absent from source checkouts.
RELEASE_MARKER = "RELEASE_BUILD"

_TRUE = ("1", "true", "yes", "on")


def is_release_build(base_dir) -> bool:
    return (Path(base_dir) / RELEASE_MARKER).exists()


def trusted_public_key(base_dir, *, debug: bool, environ) -> str:
    if is_release_build(base_dir):
        # Internal dogfood installs run on dev-signed licenses. Choosing between
        # two built-in keys gives nobody a key they can sign with.
        if environ.get("VIGIL_ACCEPT_DEV_LICENSES", "").strip().lower() in _TRUE:
            return DEV_PUBLIC_KEY
        return PRODUCTION_PUBLIC_KEY
    return environ.get("VIGIL_LICENSE_PUBLIC_KEY", DEV_PUBLIC_KEY if debug else "")
