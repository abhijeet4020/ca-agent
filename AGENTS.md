# Instructions for Coding Agents for Financial Agentic application

## About Financial Agentic application
This is an agentic ai based project, which helps chartered accountants for financial analysis, report generation and data gathering.

## Working Rules
Read and strictly follow the working rules defined in `.agents/workingrules.md`

## Path Rules
- All paths in this `AGENTS.md` are resolved relative to the directory containing `AGENTS.md`.
- Skill files (SKILL.md) resolve paths relative to their own directory.
- Scripts referenced inside a skill must use SKILL.md file relative paths (e.g., `scripts/resize.py`).
- Skills are self‑contained: scripts should normally live inside the `<skill>/scripts` directory.

## Project Folder Map
In case folder does not exist, Create the neccessary folder as needed.
Create subfolders as needed when folder is marked with $SUBFOLDERS_AS_NEEDED.

- `.agents/` : Coding Agent configuration files. Including the SKILL files. $SUBFOLDERS_AS_NEEDED
- `docs/`: project documentation
- `docs/devenv.md` : instructions to setup the development environment for the new developer
- `docs/specifications/`: requirements and specs
- `docs/specifications/specindex.md`: index of specification documents
- `docs/design/`: architecture and design decisions
- `docs/design/ARCHITECTURE.md`: architecture rules and constraints
- `docs/design/ADR.md`: architecture decision records
- `docs/implementation_plan.md` : Phase wise implementation plan of this project.
- `build/` : built tool configuration files and any temporary files generated during the compilation and build process. $SUBFOLDERS_AS_NEEDED
- `src/`: application code
- `src/ca-agent/` : main application source code. $SUBFOLDERS_AS_NEEDED
- `test` : information, plans, test source code, test data etc.
- `test/plans` : test plans are stored in this folder. $SUBFOLDERS_AS_NEEDED
- `test/unit` : unit tests source code. $SUBFOLDERS_AS_NEEDED
- `test/testdata` : all neccessary test data required for unit or integration tests. $SUBFOLDERS_AS_NEEDED

## Required Reading Before Making Any Changes
- Read Coding Agent configuration files from `.agents`
- Read SKILL files as needed from `.agents/skills`.
- Read your memories from `.agents/memory` folder
- Read `docs/implementation_plan.md` to understant the planned roadmap and timeline
- Read `docs/specifications/specindex.md`, then all relevant specification files as needed.
- Follow `docs/design/ARCHITECTURE.md`.
- Follow decisions in `docs/design/ADR.md`.
