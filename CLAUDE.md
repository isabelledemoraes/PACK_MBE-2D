# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A **pack-kit "pack"** (`sample_doc_mbe`): a specialist LLM agent that answers questions about MBE (molecular beam epitaxy) growth runs **from a knowledge graph built by reading the lab's sample sheets** (`.doc` Word files, one per sample), never from model recall. There is little application code here: the pack is mostly declarative config consumed by external tools — `pack-kit` (learn/serve/publish), `agent-slapsci` (agent runtime), and `always-on-memory` (`memory_tools`, the graph DB + n-ary extractor). Those come from `pyproject.toml` (pinned `pack-kit @ v0.7.0rc8` from gitlab.com/l_sim) and are not in this repo.

## Commands

```bash
uv sync                                          # create .venv with pack-kit (maintainer group by default)
python -m pack_kit.learn . --list                # list declared sources without running
python -m pack_kit.learn .                       # ingest sources.yaml into memory/sample_doc_mbe.db
python -m pack_kit.learn . --coreference         # merge duplicate entities (slow; for scheduled runs)
python -m pack_kit.serve .                       # web UI to inspect what actually went into the memory
agent-slapsci --validate -c config.yaml          # config check; must exit 0
agent-slapsci -c config.yaml --terminal          # talk to the user-facing (read-only) agent
agent-slapsci -c config_admin.yaml --terminal    # maintainer/curator agent (web, ingest, purge)
python fetchers/sample_doc_fetcher.py <dir>      # dry-run the fetcher: print rendered text of each sheet
```

`learn.cmd` / `serve.cmd` are the Windows wrappers; the README's `learn.sh` / `serve.sh` are symlinks into pack-kit and are not committed. There is no test suite or linter configured. `.doc` extraction needs `catdoc` (preferred), `antiword`, or on Windows Word via `pywin32`.

## Architecture

The data flow: `sources.yaml` → fetcher → `pack_kit.learn` → LLM extraction constrained by `ontology/schema.ttl` → `memory/sample_doc_mbe.db` → queried by the agent defined in `config.yaml` with persona `prompts/lead.md`.

- **`sources.yaml`** declares a single source using a Python `fetcher:` (`fetchers/sample_doc_fetcher.py::fetch_sample_docs`) with `fetcher_args` (the sheet directory — currently a Windows path) and `fetcher_env` (hash file location). The `learn:` block is passed **verbatim** to the extraction LLM; it is the main lever on memory quality.
- **`fetchers/sample_doc_fetcher.py`** must return a list of dicts with `url`, `content`, `section`, `changed`, `error` (the contract of `pack_kit.learn._pages_from_fetcher`). It converts `.doc`/`.docx`/`.txt`/`.md` to text, deterministically re-attaches labels to the header table cells (`_parse_header`; operator is read positionally after the molyblock), and prepends a rendered header. Key invariants documented in the module:
  - Change detection hashes the **rendered text**, not file bytes (re-saving a `.doc` changes metadata).
  - The body is never reordered or interpreted — line order is the only chronology.
  - Blank lines are kept (collapsed to one) because `pack_kit.learn` chunks on them.
  - `section` must name the sample (`Sample 1-0764`), not the file.
- **`ontology/schema.ttl`** (`sd:` namespace) decides what can be extracted: the n-ary extractor derives frames from `rdfs:domain` — a class that is the domain of a property becomes a pivot (e.g. `Growth`, `GrowthStep`, `RheedObservation`, `RheedSetting`, `Incident`); others are leaves. It is **descriptive only by design** (no Recipe/Prescription classes). Read its header comments before changing it.
- **`MEMORY_TRIPLET_MODE: nary`** must stay identical across all configs; changing it requires re-ingesting everything (delete `memory/*.db` and re-run learn).
- **`config.yaml`** = user agent: read-only by construction (`query_memory` only, no web, no ingest). This is a structural security property, not a setting to relax. Only `config.yaml` is loaded by `use_pack`. Memory tools are loaded via `file: "{module:memory_tools}"`, not `module:` — `file:` gives each pack its own module instance so env (`MEMORY_DB` etc.) isn't shared across packs.
- **`config_admin.yaml`** = maintainer curator: adds web, `add_text`/`add_file`/`purge_source`/`run_coreference`, and the native tool `mytools/raman/raman_peak_fit.py::analyse_raman_folder` (Raman spectra peak fitting from LabSpec6 `.txt` exports; needs numpy/pandas/scipy/lmfit/matplotlib, which are not listed in `pyproject.toml`). Note it currently uses `prompts/lead.md`, not `prompts/curator.md`.
- `config_copie_*.yaml` / `config_admin_copie_wifi.yaml` are local variants of the two configs pointing at other LLM endpoints (e.g. a local OpenAI-compatible Qwen server) instead of `cea_mistral` / maia-chat.intra.cea.fr. Keep their non-provider settings in sync with the originals.
- Any library a tool imported by `config.yaml` needs must be added to `dependencies` in `pyproject.toml`, or the published pack won't have it.

## Conventions

- Never hand-edit `memory/` (gitignored, rebuilt by learn). `sandbox/` is the agent's write area; `knowledge/` and `.venv/` are also gitignored.
- The first paragraph of `prompts/lead.md` is the pack's description everywhere (including other agents' delegation docstrings) — write it as *when to call this pack*.
- Re-ingesting a source replaces what it previously taught (idempotent). Ingest via the serve UI's "Ingest" box is lost on rebuild.
- When checking a learn run, trust the row/triplet count it prints, not the ✅ ticks (tools return failures as text); `dead_letters = 0` with 0 triplets means nothing was learned.
