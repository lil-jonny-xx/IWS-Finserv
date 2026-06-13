# Project: IWS / MIS Portal

## Graph-first navigation

A graphify knowledge graph exists at `graphify-out/graph.json` (1253 nodes, 1937 edges, 96 communities) spanning the project's code and docs. A human-browsable Obsidian vault is generated from it at `graphify-out/obsidian/` (regenerate separately if you need it current).

**Default behavior:** When asked to do anything in this project, query the graph first using `graphify query "<question>"` to understand context, relationships, and relevant files. Do NOT read files or scan directories unless:
- The user explicitly says to read a file (e.g. "open", "read", "show me the contents of")
- A graph query returns insufficient detail and a specific file read is needed to proceed
- The task is a direct file edit (in which case read only the target file)

**How to query:**
```bash
graphify query "<natural language question about the codebase>"
```

Run this from `/var/www` (the project root where `graphify-out/` lives).

**Keeping the graph current:** After making changes, run `graphify /var/www --update` to incrementally update the graph with only the changed files.
