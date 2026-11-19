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

`learn.cmd` / `serve.cmd` are the Windows wrappers; the README's `learn.sh` / `serve.sh` are symlinks into pack-kit and are not committed. There is no test suite or linter configured. Legacy `.doc` sheets need LibreOffice (`soffice`) or, on Windows, Word via `pywin32`; `catdoc`/`antiword` only as a degraded fallback. Add `--images` to the fetcher dry-run to also write the pictures.

## Architecture

The data flow: `sources.yaml` → fetcher → `pack_kit.learn` → LLM extraction constrained by `ontology/schema.ttl` → `memory/sample_doc_mbe.db` → queried by the agent defined in `config.yaml` with persona `prompts/lead.md`.

- **`sources.yaml`** declares a single source using a Python `fetcher:` (`fetchers/sample_doc_fetcher.py::fetch_sample_docs`) with `fetcher_args` (the sheet directory — currently a Windows path — and `image_dir`) and `fetcher_env` (state file of the change detection). The `learn:` block is passed **verbatim** to the LLM; with `extraction_guide.md` it is the main lever on memory quality.
- **The sheets are free notes** (three operators, three layouts, French/English mixed). Nothing in the body may be parsed by pattern; only the header table is read structurally. Quality comes from the schema descriptions, `learn:` and `extraction_guide.md` — not from regexes.
- **`fetchers/sample_doc_fetcher.py`** must return a list of dicts with `url`, `content`, `section`, `changed`, `error` (the contract of `pack_kit.learn._pages_from_fetcher`). Everything goes through the `.docx` form (`.doc` is converted by LibreOffice, else Word/pywin32; `catdoc`/`antiword` is a degraded last resort that says so in the text). Key invariants:
  - Runs highlighted in **yellow are removed**: they are template defaults the operator never filled in. Each removal is printed (`[yellow] …`).
  - Embedded pictures are written to `data/images/<sample>/<file stem>_imgNN.ext` (gitignored) and replaced in the text by `[IMAGE: <path relative to the pack>]` at the place they stood.
  - The header is the table the sheet opens with; a label's value is looked for in its own cell, then right, then below. Unlabelled cells are passed on verbatim.
  - Change detection hashes the **rendered text**, not file bytes; a file with unchanged size+mtime is not even opened. Bump `RENDER_VERSION` when the rendering changes, or unchanged files keep being skipped.
  - The body is never reordered or interpreted. Blank lines are kept (collapsed to one) because `pack_kit.learn` chunks on them. `section` must name the sample (`Sample 1-0764`), not the file.
- **Two LLM calls read each sheet** (default `preprocess`): a *filter* call that rewrites the sheet as explicit sentences, given `ontology_digest` + `extraction_guide.md` + `about:` + `learn:`; then the n-ary extractor (fact enumeration, then mapping onto the full schema).
- **`ontology/schema.ttl`** (`sd:` namespace) decides what can be extracted. Read its header before changing it; the constraints that are easy to break:
  - A class that is the `rdfs:domain` of a property is a pivot/frame (`Growth`, `GrowthStep`, `SourceUsage`, `Calibration`, `RheedObservation`, `RheedSetting`, `Incident`, `Characterization`, `RamanPeak`); others are leaves. Only the first `rdfs:domain` is read — a property shared by several frames needs an `owl:unionOf` domain.
  - The filter sees one line per class: label + **first sentence** of its comment (cut at the first `". "`, 150 chars), the whole digest cut at **3500 chars**. Keep first sentences short and self-sufficient.
  - The extractor sees every class/property with its full `rdfs:comment`; properties are offered under their **local name**, classes under their `rdfs:label`. `#` comments, `skos:altLabel` and **individuals are invisible** to both calls — so the closed `StepKind` list is spelled out in the class comment and in `learn:` (keep the three in sync), and synonyms go into comments.
  - A step carries no source parameter: each source open during a step is one `SourceUsage` (co-evaporation = several).
  - **The answering agent never sees the class of a leaf**: `query_memory` (aom_query) renders `- <pivot> (<class>)` then `role → <target label>`. Any distinction a question must be able to see has to be a *role of a pivot* — hence `sourceKind` (effusion cell / electron gun) on `SourceUsage` instead of subclasses of `Source`, which were stored but invisible and got the two confused. `prompts/lead.md` tells the agent how to read these roles.
  - **Every child pivot is linked both ways** (`hasStep`/`partOfGrowth`, `hasSource`/`usedInStep`, …). The extractor builds top-down; a child that can only point up to its parent ends up detached (seen on real sheets: sources floating with their literals, attached to no step). Units live on the property where the sheets agree (°C, mbar, %); durations, thicknesses and rates are strings "as written". Nothing is converted or recomputed.
  - It is **descriptive only by design** (no Recipe/Prescription classes).
- **`MEMORY_TRIPLET_MODE: nary`** must stay identical across all configs; changing it requires re-ingesting everything (delete `memory/*.db` and re-run learn).
- **`config.yaml`** = user agent: read-only by construction (`query_memory` only, no web, no ingest). This is a structural security property, not a setting to relax. Only `config.yaml` is loaded by `use_pack`. Memory tools are loaded via `file: "{module:memory_tools}"`, not `module:` — `file:` gives each pack its own module instance so env (`MEMORY_DB` etc.) isn't shared across packs.
- **`config_admin.yaml`** = maintainer curator: adds web, `add_text`/`add_file`/`purge_source`/`run_coreference`, and the native tool `mytools/raman/raman_peak_fit.py::analyse_raman_folder` (Raman spectra peak fitting from LabSpec6 `.txt` exports; needs numpy/pandas/scipy/lmfit/matplotlib, which are not listed in `pyproject.toml`). Note it currently uses `prompts/lead.md`, not `prompts/curator.md`.
- `config_copie_*.yaml` / `config_admin_copie_wifi.yaml` are local variants of the two configs pointing at other LLM endpoints (e.g. a local OpenAI-compatible Qwen server) instead of `cea_mistral` / maia-chat.intra.cea.fr. Keep their non-provider settings in sync with the originals.
- **Local Qwen through a slapsci-server** (`provider: openai`, `model: local_qwen3_coder_next//<path.gguf>`, `api_url:`): the extraction calls send no `max_tokens`, so the server's default applies — 8192 tokens per reply, *thinking included*. A sheet whose text stops mid-value in `memories.raw_text` was cut by that limit. It is not a pack setting: set `SLAPSCI_QWEN3_CODER_NEXT_MAX_TOKENS` (and, if needed, `SLAPSCI_QWEN3_CODER_NEXT_N_CTX`, `SLAPSCI_QWEN3_CODER_NEXT_ENABLE_THINKING=0`) in the environment of the machine that runs `slapsci-server`, then restart it. `MEMORY_*` variables cannot carry it.
- Any library a tool imported by `config.yaml` needs must be added to `dependencies` in `pyproject.toml`, or the published pack won't have it.

## Conventions

- Never hand-edit `memory/` (gitignored, rebuilt by learn). To rebuild after a schema change, delete the whole `memory/` directory, not only the `.db`: `memory/sample_doc_hashes.json` is the fetcher's change-detection state, and with it left in place every sheet is reported unchanged and skipped. `sandbox/` is the agent's write area; `knowledge/` and `.venv/` are also gitignored.
- The first paragraph of `prompts/lead.md` is the pack's description everywhere (including other agents' delegation docstrings) — write it as *when to call this pack*.
- Re-ingesting a source replaces what it previously taught (idempotent). Ingest via the serve UI's "Ingest" box is lost on rebuild.
- When checking a learn run, trust the row/triplet count it prints, not the ✅ ticks (tools return failures as text); `dead_letters = 0` with 0 triplets means nothing was learned.
