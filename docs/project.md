# project.md — Pokestrator

## Context

This is a hackathon project where **Poke automatically improves its own capabilities** when it hits limitations (missing integrations, repeated failure patterns, poor retrieval on long docs, etc.). When that happens, Poke calls your **MCP server** (deployed on Render) which runs an **orchestrator agent**.

The orchestrator:
1) receives either **a description of the limitation**,
2) decides whether a previously-created "subagent" can solve it,
3) if so, runs it using the subagent's spec from the DB (using a custom function to integrate it with Claude Agent SDK),
4) if not, **creates a new subagent** specialized for that need (e.g., "Stripe analyst"), persists it, and makes it usable going forward.

Note that:
- Poke can connect to any MCP server via an MCP Server URL (and optional API key) you create in its Integration Library.  
  Source: https://poke.com/docs/managing-integrations
- Poke can set up its own automations (on its own platform, not related to this codebase) to call the MCP server when it needs to (detecting limitations).

---

## Design principle (important)

**Subagents should be "agents-as-data," not new code deployments.**

A subagent is a **versioned configuration package** stored in Render PostgreSQL:
- name + description (used for routing)
- system prompt / playbook
- optional connector configs (e.g., OpenAPIProvider config)
- observed success metrics
This schema is just a suggestion for now, it should end up being whatever is most compatible with the Claude Agent SDK to run and most accurate (the subagent is actually able to solve the queries it was created for).

Suggested minimal schema (CAN BE CHANGED FOR WHATEVER IS BETTER):

- `id` (uuid)
- `name` (string) — e.g., `stripe_analyst`
- `description` (string) — natural-language summary of what this subagent does (used for routing)
- `prompt_md`: system instructions/playbook
- `connectors` (optional):
  - OpenAPIProvider configs
  - ProxyProvider endpoints (remote MCP servers)
  - auth requirements
- `metrics` (implement later, after core product):
  - uses, success rate, avg latency, last_used

---

## System architecture

### 1) Poke ↔ Pokestrator MCP server (single permanent integration)

You create a single Poke custom integration:
- Name: `Pokestrator`
- MCP Server URL: `https://<your-render-service>/sse`

Poke calls a **single stable entry tool** such as:
- `pokestrator.orchestrate(task_description)`

### 2) Pokestrator MCP server components

**A. Orchestrator (router + lifecycle manager)**
- Input: task description
- Output: completed answer/action

**B. Subagent creator**
- Constructs new SubagentSpec when needed
- Assumes API keys are already available in the environment (we can hardcode them for now)
- We will have to experiment to see what works best to actually solve the task, could involve
  - finding existing MCP servers or APIs to use
  - finding the right library which the agent can run custom code to solve the task
  - etc.
- Persists the SubagentSpec to the DB

**C. Subagent runner**
- Spawns an agent instance with the subagent's spec from the DB
- Runs the agent
- Returns the result and sends it back to Poke via it's POST API (can be simply logged for now, integration with Poke should be straightforward)

---

## Demo script (what judges will remember)

1) You ask: "Pull Stripe revenue by product and summarize weekly trend."
2) Pokestrator: "I can't—no Stripe capability yet." (internally sends a report to the orchestrator)
3) Pokestrator silently builds `stripe_analyst` subagent.
4) You ask the same thing again.
5) This time, `stripe_analyst` runs and returns the analysis.

The "wow" is that the second attempt succeeds without you adding anything manually.
