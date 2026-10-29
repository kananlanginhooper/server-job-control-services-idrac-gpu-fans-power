# Moving a server from hand-installed services to this repo

How to switch a server's live helper services to the units in this repo, one at a time, with a 10-second rollback.

1. **Clone and configure.** Clone the repo on the server, copy the shared `.env` next to it (`chmod 600`), and check what this server resolves:
   ```bash
   python3 scripts/show-env.py $(hostname)
   ```
   Secrets show as `(set)`.
2. **Keep the live unit names.** Install the repo unit under the name the server already uses, so the dashboard, power hooks and existing drop-ins keep working:
   ```bash
   scripts/install-units.sh services/<svc>/<unit>=<live unit name>
   ```
   The script backs up the file it replaces as `<name>.pre-repo-<stamp>` and runs `daemon-reload`. It never starts anything.
3. **Keep the data paths.** Services read and write the same `/run` and `/dev/shm` files as before. Settings that differ per server go in `.env` as `<HOST>_KEY` lines. For example, `<HOST>_POWER_GUARD_DIR` keeps power-guard's config, state and log where other tools read them.
4. **One at a time.** Restart one unit, check its output file, endpoint or journal, then move on. Restart fan-control last: while it restarts, the fans go to Dell automatic (its exit path), which is safe.
5. **Rollback:**
   ```bash
   sudo cp /etc/systemd/system/<name>.pre-repo-<stamp> /etc/systemd/system/<name> && sudo systemctl daemon-reload && sudo systemctl restart <name>
   ```
   The old code files aren't touched by the cutover, so the old unit comes back exactly as it was.
6. **Order dependencies.** Where a service needs something prepared at boot (tools restored into `/dev/shm`, say), add a small drop-in with `After=`/`Wants=` for that unit, rather than editing the template.
