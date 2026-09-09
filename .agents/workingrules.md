# Coding Agent Working Rules
Strictly follow these rules. DO NOT VIOLATE UNDER ANY CIRCUMSTANCES.

# Working Rules
- Make small, focused changes.
- Preserve behavior unless the task explicitly requires a change.
- **Do not change public APIs** unless instructed.
- Follow existing project patterns before introducing new ones.
- Generate the "plan" BEFORE making any changes, updating any documents or generating any code/tests.
- Show the plan to me (the user) and get my confirmation. 
- Write the unit tests before making any changes.
- Run the unit tests and ensure that all tests are passing after making any changes.
- Modify files in `docs/` folder only when explicitly told by user. Do not modify on your own.
- Store your memories in `.agents/memory/` folder.

### DO NOT
- implement anything that conflicts with approved specs or architecture decisions.
- Rewrite large parts of the codebase unless explicitly asked.
- Reformat unrelated files.
- Remove TODOs/comments without addressing their intent.
- Assume undocumented behavior is safe to change.
- Generate or modify files that I did not explicitly ask and are not part of generated plan

### When Unsure
- Ask for clarification instead of guessing.
- Briefly state trade-offs in review notes.

## Code Generation Instructions
- Analyze the changes in specifications and design documents using version control diff, identify required code changes/additions/deletions, and implement only those changes in source code.
- Use the 'thinking craftsman skill' and generate the code compliant with the Thinking Craftsman Coding Guidelines
- Follow this project's coding guidelines first, then apply thinking craftsman guidelines.

### Generating new source code file.
- Add the "Purpose" code comment to start of any new source code file that you generate. Describe what is the purpose of this file or class implemented/declared in this file.
- Add an entry for the new source code file in `docs/design/sourcemap.md`. The entry must contain the name of the source code file (path relative to project root) and the purpose of the file.

### Purpose Comment
- "Purpose" comment is short (maximum 4-5 lines)
- "Purpose" comment describes need, what is implemented, any particular algorithms or data structures used. 
- **Bad Purpose Comment**
    - This file implements FeatureCache class
- **Good Purpose Comment**
    - A feature cache is required for improving the performance. FeatureCache class implements it. FeatureCache class uses LRU algorithm for caching and uses custom dictionary implementation.

### Modifying the existing files
- Use information from `sourcemap.md` to decide which existing files to modify.

## Code Review Instructions
- Use the  'thinking craftsman skill' for reviewing code and ensure that code is compliant with 'thinking craftsman coding guidelines' 
- Follow this project's review guidelines first, then apply thinking craftsman guidelines.

## Executing Commands with Environment Configuration

Always chain the environment setup script (environment.bat or environment.sh) before executing commands to ensure required variables and paths are properly configured.

### Windows (PowerShell/pwsh)
```powershell
# Basic command execution with environment setup
.\environment.bat && command-here

# Examples:
.\environment.bat && ant build
.\environment.bat && ant clean build test
.\environment.bat && java -version
```

### Unix/Linux (Bash)
```bash
# Basic command execution with environment setup
source ./environment.sh && command-here

# Examples:
source ./environment.sh && python -m unittest tests/
source ./environment.sh && python src/aicalc/main.py
```

### Best Practices
- **Always chain before executing**: Use `&&` (PowerShell/Bash) to ensure environment loads before the command runs.
- **Verify environment**: After running the environment script, it should display confirmation output (e.g., "Environment configured").
- **Project-specific setup**: Navigate to the project root before executing the environment script.
- **CI/CD pipelines**: Include the environment chaining step in all build and test scripts.
    
    