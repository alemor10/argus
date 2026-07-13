# Argus

Personal equity monitor + discovery tool, named for Argus Panoptes — the
hundred-eyed watchman. Some eyes always open; watches and reports, never acts.

- `argus watch` — monitor a watchlist, detect changes, produce a thesis-aware digest
- `argus scout` — screen a broad universe for new candidates (proposes only)

Read-only by design: no trading, no predictions, no autonomous decisions.

See [CLAUDE.md](CLAUDE.md) for the roadmap, data-source decisions, and hard constraints.

## Dev

Uses [uv](https://docs.astral.sh/uv/):

```sh
uv sync          # install deps
uv run argus     # run (once the CLI exists)
uv run pytest    # tests
```
