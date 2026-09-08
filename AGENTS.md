# AGENTS — hermes-identity

## Purpose
Keep Hermes persona + memory identical across machines without syncing sessions or secrets.

## Rules
1. Only edit/commit: `SOUL.md`, `memories/MEMORY.md`, `memories/USER.md`.
2. Never commit `~/.hermes/.env`, `auth.json`, session DBs, cache, or API keys.
3. On each machine, identity files are symlinks into a local clone of this repo.
4. After Hermes rewrites MEMORY/USER, run `hermes-sync-identity.sh push` before switching machines.
5. Before starting Hermes on a cold machine, run `hermes-sync-identity.sh pull`.

## Layout
```
~/src/hermes-identity/          # git clone
~/.hermes/SOUL.md               → ~/src/hermes-identity/SOUL.md
~/.hermes/memories/MEMORY.md    → ~/src/hermes-identity/memories/MEMORY.md
~/.hermes/memories/USER.md      → ~/src/hermes-identity/memories/USER.md
```
