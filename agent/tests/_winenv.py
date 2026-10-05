r"""Windows-path logic on a POSIX host.

These collectors are Windows-only code: on the suite's Linux host
``sys.platform`` says ``linux``, so the branches under test never run at all.
Everything else can stay in the host's own spelling — the functions only glob,
stat and join the paths they are handed, never asking what kind of path a path
is — so a fixture built as a real temp tree gives the same string to the code
and to the assertion.

What the ``with`` block patches is therefore only what the code asks the
*process*: ``sys.platform``, and the two environment entries it reads. PATH is
fixed rather than inherited: the host's names directories that exist here, which
would let a per-user alias path pass for a package path and leave the assertions
host-dependent.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

OLD_PATH = ["/usr/bin", "/usr/local/bin"]

#: The VM's own package directory names, 2026-09-29.
APP_INSTALLER = "Microsoft.DesktopAppInstaller_1.29.379.0_x64__8wekyb3d8bbwe"
VCLIBS_OLD = "Microsoft.VCLibs.140.00.UWPDesktop_14.0.33519.0_x64__8wekyb3d8bbwe"
VCLIBS_NEW = "Microsoft.VCLibs.140.00.UWPDesktop_14.0.33728.0_x64__8wekyb3d8bbwe"
#: An x86 runtime with the newest version number of the three: the glob is
#: pinned to ``_x64__``, so it must not win the descending sort.
VCLIBS_X86 = "Microsoft.VCLibs.140.00.UWPDesktop_14.0.99999.0_x86__8wekyb3d8bbwe"


class FakeProgramFiles:
    r"""A temp ``%ProgramFiles%`` with the machine-wide package layout.

    ``install`` takes the trailing segments of a package path as the VM spells
    them — ``Microsoft.DesktopAppInstaller_…\winget.exe`` — and makes the same
    directories and files here. ``package`` and ``winget`` give back the string
    the code under test builds for the same location, because both join against
    this fixture's root.
    """

    def __init__(self, tmp: str):
        self.root = Path(tmp)
        self.apps = self.root / "WindowsApps"

    def inside(self, *segments: str) -> str:
        """The fixture path for the given package segments."""
        parts: list[str] = []
        for segment in segments:
            parts.extend(p for p in segment.replace("/", "\\").split("\\") if p)
        return str(self.apps.joinpath(*parts))

    def install(self, *segments: str) -> "FakeProgramFiles":
        """Create each named package; a trailing ``.exe`` makes it a file."""
        for segment in segments:
            target = Path(self.inside(segment))
            if segment.endswith(".exe"):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("")
            else:
                target.mkdir(parents=True, exist_ok=True)
        return self

    def package(self, name: str) -> str:
        return self.inside(name)

    def winget(self) -> str:
        return self.inside(APP_INSTALLER, "winget.exe")

    def per_user_winget(self) -> str:
        """An App Execution Alias path — ``WindowsApps`` in it, not the package."""
        return str(self.root / "Users" / "u" / "AppData" / "Local"
                   / "Microsoft" / "WindowsApps" / "winget.exe")


class windows_machine:
    """Run one module's logic as though the process were on Windows."""

    def __init__(self, module, program_files: FakeProgramFiles):
        self._module = module
        self._program_files = program_files

    def __enter__(self):
        self._environ = patch.dict(os.environ, {
            "ProgramFiles": str(self._program_files.root),
            "PATH": os.pathsep.join(OLD_PATH),
        })
        self._environ.start()
        self._patch = patch.object(sys, "platform", "win32")
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self._environ.stop()
        return False
