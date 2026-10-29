# Link pusher

**Runs on:** every server (as the service user), as one continuous task-runner row

Moves files between servers by the push-link standard (`SERVER-LINKS.md` in the server-projects folder). Each server has an outbox `/link/<self>-<to>` and an inbox `/link/<from>-<self>` for every other server. The pusher watches this server's outboxes. When files are published there, it opens the connection itself and pushes them with `rsync` over SSH into the same folder on the receiver, then removes the sent copies. Nothing mounts, pulls or keeps a connection open.

| | |
|---|---|
| Reads | `/link/<self>-<to>/` (outboxes), `/proc/mounts`. |
| Writes | The receiver's `/link/<self>-<to>/` via the `<to>-link` SSH alias; removes sent files from the outbox; `LINK_STATUS_FILE`; log lines on stdout (the task runner's log). |
| Settings | `LINK_TARGETS` (per server: `<to>:<cap KB/s>` list, 0 = uncapped), `LINK_SERIAL`, `LINK_ROOT`, `LINK_SWEEP_S`, `LINK_STATUS_FILE`, `LINK_WARN_GB`, `LINK_WARN_AGE_H`, `LINK_IO_TIMEOUT_S`, `LINK_SETTLE_S`. |
| Needs | `rsync`, `inotify-tools`; the link key `~/.ssh/link_<self>_ed25519` and a `<to>-link` alias per receiver; on each receiver, an `authorized_keys` line `command="/usr/bin/rrsync -wo /link/<from>-<to>",restrict <pubkey>`. |

Settings come from the repo's one `.env` (see [`.env.example`](../../.env.example)).

## Sending files

1. Write into `/link/<self>-<to>/.incoming/` (or as `name.part`).
2. When it's complete, `mv` it into `/link/<self>-<to>/`. The rename is the publish.
3. For a set that only makes sense whole, publish the parts first and an empty `<set>.done` last. The pusher sends markers in a second pass, and only markers that existed before the first pass began, so a marker never arrives before its parts.

On the receiver, the consumer takes files out of `/link/<from>-<self>/` and moves or deletes them. Delivery is **at least once**, so consumers must be idempotent.

## How it runs

- One thread per outbox. `inotifywait` (`close_write`, `moved_to`) wakes it; a full sweep runs every `LINK_SWEEP_S` and at start. After a wake it waits 2 s so a burst goes in one push.
- Push = `rsync -a --partial-dir=.rsync-partial --remove-source-files --chmod=... [--bwlimit=cap] <outbox>/ <to>-link:`. Dotfiles, dot folders (`.incoming`, `.rsync-partial`) and `*.part` are never sent. The target is the inbox root (`<to>-link:`) because rrsync roots the key there.
- **Only settled files are pushed** (defense in depth, after a 2026-10-29 incident where a producer wrote straight into an outbox and lost most of a file). A file changed in the last `LINK_SETTLE_S` (default 60 s), or open for writing by any process the service user can see in `/proc`, is held and retried once it can have settled. While anything is held, `.done` markers wait too. Each push sends an explicit `--files-from` list, never the whole folder, so nothing outside that list is ever removed.
- A failed push backs off 30 s, doubling to 15 min. Files stay in the outbox until they've arrived whole.
- `LINK_SERIAL=1` lets only one outbox push at a time, so a server on a capped link stays under its outbound cap.
- **Never pushes from a network mount.** If an outbox is on NFS/CIFS/sshfs, it refuses: `--remove-source-files` would delete the only copy on the far side.
- Empty subfolders left by `--remove-source-files` are removed after 10 min.

## Status

`LINK_STATUS_FILE` (default `~/link-pusher/status.json`), rewritten every 30 s:

```json
{"host": "hub", "at": 1761771600.0,
 "outboxes": {"hub-desk": {"target": "desk", "cap_kbps": 1000, "files": 3, "bytes": 52428800, "oldest_age_s": 40,
   "last_push_at": 1761771590.1, "last_ok_at": 1761771590.9, "last_error": null, "last_files": 2, "last_bytes": 1048576,
   "last_rate_Bps": 1001233, "backoff_until": null, "pushing": true, "warn": false}},
 "inboxes": {"desk-hub": {"files": 0, "bytes": 0, "oldest_age_s": null, "fs": "ext4"}}}
```

`warn` is true when an outbox holds more than `LINK_WARN_GB` or its oldest file is older than `LINK_WARN_AGE_H`.

## Task-runner row

```bash
taskctl add link-pusher --kind continuous --desc 'push /link outboxes to the other servers (SERVER-LINKS.md)' \
  --cmd 'exec python3 ~/server-projects/server-job-control-services-idrac-gpu-fans-power/services/link-pusher/link_pusher.py'
```

Logs: `taskctl log link-pusher`. Stop: `taskctl disable link-pusher`.
