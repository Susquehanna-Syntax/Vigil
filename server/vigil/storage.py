"""Static file storage for the image (VIGIL_HASHED_STATIC, see settings.py)."""
from whitenoise.storage import CompressedManifestStaticFilesStorage


def _without_source_maps(patterns):
    def keep(p):
        return "sourceMappingURL" not in (p if isinstance(p, str) else p[0])
    return tuple((ext, tuple(p for p in pats if keep(p))) for ext, pats in patterns)


class HashedStaticStorage(CompressedManifestStaticFilesStorage):
    """Content-hashed names, without rewriting source-map comments.

    The vendored libraries (js/vendor/) end in a sourceMappingURL to a .map
    file Vigil does not ship; rewriting that comment would fail collectstatic
    on a file nobody loads. CSS url() and @import are still rewritten."""

    patterns = _without_source_maps(CompressedManifestStaticFilesStorage.patterns)
