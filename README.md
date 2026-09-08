# hermes-identity

Shared Hermes identity across Matheus's Mac and Ailla's machine.

## Synced (only)

- `SOUL.md` → `~/.hermes/SOUL.md`
- `memories/MEMORY.md` → `~/.hermes/memories/MEMORY.md`
- `memories/USER.md` → `~/.hermes/memories/USER.md`

Local only: sessions, cache, logs, `.env`, `auth.json`, rest of `~/.hermes/`.

## Sync

```bash
~/bin/hermes-sync-identity.sh pull
~/bin/hermes-sync-identity.sh push
```

See `AGENTS.md`.
