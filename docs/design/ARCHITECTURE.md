# ARCHITECTURE OF Financial Agentic application

## Key Architecture Guidelines
Always follow the decisions in `ADR.md`

### Modules
- each folder in the `src/ca-agent` represents a module or a submodule
- modules must have acyclic dependency
- Higher layer modules can depend on lower layer module
- Modules in the Same layer cannot depend on each other

### Implementation guidelines
- Generate the top level file (containing the main or entry point function) for application in the `src/` folder.
- Generate and update package design diagram in `docs/design/packagedesign.md` when a new package is added to the system. The file will be in markdown format and diagrams in mermaid.js format.

## Technology Stack
backend : python fastapi
data-pipeline : Python 3.10.9
frontend : python streamlit
build tool : uv 
unit test framework : pytest
Other tools/libraries/frameworks : (langgraph, langchain)
package managers: uv

## Technolgy stack specific instructions for Code generation
- UI will be created in streamlit. Agents will be created using langgraph, data pipeline will be made of medallion architecture with bronze, silver and gold layer (used by agents to read data).
- Never read all the documents given in any index document, gather requried information from index file and read required documents as per given path

## Development Setup
IDE : VS code

## Design Documents
- `docs/design/sourcemap.md` : list of source code files and their purpose. Use this information to decide which files to modify or update during the code generation.
