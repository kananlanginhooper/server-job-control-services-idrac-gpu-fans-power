# SSD write cache

**Runs on:** hub (root, boot)

Boot oneshot: turns on the drive write cache (`sdparm --set WCE=1`) for the listed data SSDs, matched by serial, because sdX names change. Leave out the OS disk and any drive that misbehaves with the cache on.

| | |
|---|---|
| Reads | `lsblk` serials. |
| Writes | Drive settings. |
| Settings | `SSD_WRITE_CACHE_SERIALS` (space-separated, per host). |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)). The unit file(s) here are templates: `scripts/install-units.sh` renders `@REPO@`/`@USER@` and installs them. It doesn't enable or start them.
