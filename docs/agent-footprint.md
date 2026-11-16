# What the agent costs a host

Measured by the agent itself: `vigil-agent measure` runs the same local collection a check-in runs, and a software
inventory scan, and reports what they cost. It reads no config and sends nothing, so it is safe to run on any host,
installed or not. The release build runs it on every platform and prints the row in the run's summary; this page
keeps the table.

```
sudo vigil-agent measure            # a readable report
vigil-agent measure --row           # one row for the table below
vigil-agent measure --json
```

| Platform | Build | Installed | Start-up | Memory at rest | Check-in CPU | Check-in wall | Software scan | Scan peak memory |
|---|---|---|---|---|---|---|---|---|
| Linux 6.12 x86_64 (Debian 13, 2026.16.0 + M8) | binary | 14.8 MB | 0.50 s | 42.9 MB | 40 ms | 102 ms | 1.0 s | 506.7 MB |
| Linux 6.12 x86_64 (Debian 13, 2026.16.0 + M8) | source | - | 0.35 s | 36.8 MB | 30 ms | 98 ms | 1.0 s | 519.2 MB |
| Linux 6.12 x86_64 (Debian 13, 2026.16.0) | binary | 14.8 MB | 0.50 s | 42.8 MB | 30 ms | 1100 ms | 1.0 s | 515.6 MB |

The other platforms' rows come from the release build's "Measure the binary" step (macOS arm64, Windows amd64, Linux
arm64) and are added here as each release is cut.

## What the columns mean

- **Installed**: the one file, or the whole folder on Windows. A one-file build also unpacks itself into a temporary
  folder at each start (27.9 MB on Linux); the readable report shows that figure.
- **Start-up**: from launch to the first collection, the unpacking included.
- **Memory at rest**: the agent's resident memory after a check-in's collection has run. This is what it holds
  between check-ins.
- **Check-in CPU / wall**: one ordinary check-in's local collection (metrics, top processes, containers, reboot
  flag, network map), the median of five. The network round trip is not in it.
- **Software scan**: the installed-software inventory, which runs every 6 hours by default
  (`software_interval`). Its memory is mostly the package managers it asks; the peak counts them.

## What measuring found

Two things, both fixed in the same change that added the command:

1. **A check-in waited a second to sample CPU.** `psutil.cpu_percent(interval=1)` blocked every check-in for one
   second, and the "total" figure beside it was measured over a different window. Both are now the average since the
   previous check-in: a burst between two check-ins is in the number instead of missed, and the collection takes
   about 100 ms instead of 1,100 ms. Only an agent's first reading still takes the one-second sample.
2. **`flatpak remote-ls --updates` uses about 500 MB for a moment.** Flatpak loads a whole remote's index to answer,
   and has no lighter way to ask. The agent now skips that lookup when no flatpak app is installed, and when the
   host has less than 1 GB of memory available (the scan then says so, and the installed list is still reported).
   Hosts without flatpak are unaffected: on those the scan's peak is the package manager's own (about 70 MB for
   `apt list --upgradable`).
