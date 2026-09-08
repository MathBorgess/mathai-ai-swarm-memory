---
name: hermes-identity-sync
description: Sync Hermes SOUL/MEMORY/USER across machines via the hermes-identity git repo. Use when switching machines, after memory updates, or when identity drifts.
---
# hermes-identity-sync

Run from any machine with `gh`/`git` auth to `MathBorgess/hermes-identity`:

```bash
~/bin/hermes-sync-identity.sh pull   # before starting Hermes cold
~/bin/hermes-sync-identity.sh push   # after Hermes rewrote MEMORY/USER
~/bin/hermes-sync-identity.sh link   # symlink only
```

Never sync `.env`, `auth.json`, sessions, or cache.
