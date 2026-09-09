# Financial Agentic application Development Environment Setup

Use this checklist for onboarding a new developer:

1. Review the tech stack in `docs/design/ARCHITECTURE.md`.
2. Verify required tools are installed (compiler/interpreter/build tools/dependencies).
3. If anything is missing:
    - Ask whether it is already installed and where.
    - If not installed, guide setup of required software and packages.
4. Create `environment.bat` (Windows) or `environment.sh` (Unix) in the project root.
5. Ensure the environment file:
    - Adds required tool paths (e.g compiler, build tool, testing tools).
    - Sets required environment variables.
    - Defines repository paths (root, source, test).
    - Includes any stack-specific best practices.
6. Do not commit the environment file; add it to version control ignore rules.

