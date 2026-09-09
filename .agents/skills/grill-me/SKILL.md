---
name: grill-me
description: Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions "grill me". (Copied from Grill-me skill of mattpocock
origin: https://github.com/mattpocock/skills/tree/main/skills/productivity/grill-me
author:
    - mattpocock (updated by Nitin Bhide)
license: MIT
---
Interview me relentlessly about every aspect of this document until we reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one. For each question, provide your recommended answer.

Ask the questions one at a time.

If a question can be answered by exploring the codebase, explore the codebase instead. Refer the ARCHITECTURE.md , ADR.md and source map wherever possible

If this is source code file (e.g. python, c++, java, typescript, javascript, rust, html etc), then do not ask questions. Tell your user that you cannot analyze source code files and quit.

