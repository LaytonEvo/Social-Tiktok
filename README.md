# evo-tiktok-ops

The handover pack for building Evolution Golf's TikTok content jobs with Claude Code.

## Start here
1. Create a private repo and copy this folder into it.
2. Open it in Claude Code. `CLAUDE.md` loads automatically as the project brief.
3. First prompt to Claude Code:
   > Read CLAUDE.md and docs/SPEC.md. Propose a plan for milestone M0 (repo skeleton, config loading, Shopify bulk read with the exclusions and categoriser, validators with tests). List any questions from SPEC.md section 8 you need answered first.
4. Work milestone by milestone (SPEC.md section 1). Each milestone ends with its acceptance tests passing in `--dry-run`.

## Contents
- `CLAUDE.md`: standing brief and guardrails.
- `docs/SPEC.md`: architecture, data sources, jobs, tests and milestones.
- `docs/CONTENT_RULES.md`: voice, formats, price and brand rules, giveaway wording.
- `prompts/`: starter prompts for the three jobs.
- `config/`: example settings and environment variables.
- `data/evo_clearance_allocation.xlsx`: the 1 October 2026 stock allocation and routing rules to port.
