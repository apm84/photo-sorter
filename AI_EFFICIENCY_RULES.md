# AI-Assisted Development — Efficiency, Cost Optimisation and Project Continuity Rules

## Purpose

These instructions apply whenever an AI assistant is involved in planning, building, reviewing, debugging, maintaining or extending a software project.

The objective is to **minimise unnecessary token consumption, API costs, repeated work and development time**, while maintaining high standards of code quality, security, reliability and maintainability.

Apply these principles automatically to all new and existing projects, without requiring repeated instructions from the user.

## 1. Establish Project Context

Before beginning development:

- Identify the correct repository, active branch, existing pull requests and current development status.
- Read relevant project instructions, such as `AGENTS.md`, `CLAUDE.md`, `README.md`, architecture documents and development roadmaps.
- Examine existing implementations before proposing new ones.
- Identify completed work, approved decisions, unresolved issues and outstanding tasks.
- Continue existing work wherever practical rather than unnecessarily rebuilding or restarting it.
- Treat the actual repository state and current project documentation as authoritative.

Avoid reading the entire repository when targeted inspection will provide sufficient context.

## 2. Optimise Input Tokens and Context Usage

- Take advantage of automatic prompt caching, context reuse and equivalent provider-supported mechanisms where available.
- Keep stable project instructions and architectural information consistent to improve caching opportunities.
- Reuse relevant context within an existing development session when practical.
- Prefer targeted file searches, diffs and relevant code sections over repeatedly loading entire files or repositories.
- Avoid sending unnecessary logs, generated files, duplicate instructions or unrelated project information.
- Use concise summaries and references to existing documentation rather than repeatedly providing lengthy explanations.
- Manage context windows carefully, using summarisation or compaction when beneficial.
- Start a fresh session when accumulated context becomes unreliable, excessively large or counterproductive.

Recognise that prompt caching is provider-dependent, temporary and not guaranteed.

## 3. Reduce Output Tokens and Repeated Reasoning

- Avoid regenerating code, explanations, implementation plans or decisions that already exist.
- Prefer focused code patches over rewriting complete files unnecessarily.
- Preserve approved architectural decisions, design standards and implementation approaches in project documentation.
- Refer to those records instead of repeatedly reconsidering settled questions without new evidence.
- Keep progress reports, tool outputs and debugging summaries concise and useful.
- Use the level of model capability and reasoning effort appropriate to the complexity of each task.
- Reserve more expensive models or intensive reasoning modes for difficult architecture, security, complex debugging and high-risk decisions.
- Avoid repeated unsuccessful tool calls or debugging loops without investigating the underlying cause.

Previously generated content should be preserved as reusable project knowledge wherever practical.

## 4. Maintain Persistent Development Memory

For substantial development tasks, maintain a concise handover record within the project repository.

Where appropriate, use an existing development journal or a dedicated file such as `docs/AI_DEVELOPMENT_HANDOVER.md`.

Record:

- Current objectives and development status.
- Active branch, pull request and relevant commits.
- Completed features and implementation decisions.
- Important architecture and design choices.
- Files or modules changed.
- Tests performed and their results.
- Known bugs, outstanding tasks and blockers.
- Recommended next steps.
- Deployment or release status.

Update this record at meaningful checkpoints and before concluding substantial development sessions.

Keep documentation accurate, concise and free of unnecessary duplication.

Do not store passwords, private credentials, sensitive data or unnecessary conversation transcripts.

## 5. Improve Development Workflow Efficiency

- Break complex features into logical, manageable development stages.
- Plan sufficiently before implementation to minimise avoidable rework.
- Reuse existing functions, components, libraries and tests wherever appropriate.
- Make related changes together when this is safe and improves efficiency.
- Run focused tests first when appropriate, followed by broader testing according to project requirements and risk.
- Avoid unnecessary full builds, repeated installations and redundant test runs.
- Investigate recurring failures rather than repeatedly attempting the same unsuccessful operation.
- Keep changes appropriately scoped and reviewable.
- Use version control to preserve progress and support reliable recovery.

Efficiency must never become an excuse for inadequate testing or incomplete implementation.

## 6. Track Token Usage and Development Costs

Where the AI provider or development tools expose usage information:

- Track input tokens, cached input tokens, output tokens and reasoning tokens where available.
- Record actual costs or credit consumption when reported.
- Distinguish measured usage from estimates.
- Identify unusually expensive operations or repeated work.
- Recommend practical improvements that could reduce future costs.
- Consider whether a less expensive model or workflow could achieve equivalent quality.

Never invent token counts, cache-hit rates, costs or usage statistics.

Evaluate optimisation based on the total cost of achieving a correct, completed result, rather than minimising tokens in individual requests.

## 7. Apply These Rules to Other AI Agents

When preparing instructions for another AI coding agent, development platform or automated workflow:

- Include the relevant efficiency and continuity requirements.
- Direct the agent to consult existing project documentation.
- Encourage reuse of previously completed work and approved decisions.
- Require targeted context retrieval and focused implementation.
- Ensure meaningful progress and decisions are recorded for future sessions.
- Avoid duplicating lengthy instructions when referencing existing guidance is sufficient.
- Maintain consistent project standards across different AI providers and development environments.

These principles should operate across tools and platforms, not depend on a particular AI vendor.

## 8. Preserve Quality, Security and User Control

Token and cost optimisation must never take priority over:

- Application correctness and reliability.
- Security, privacy and data integrity.
- Appropriate testing and validation.
- Financial and transactional accuracy.
- Accessibility and user experience.
- Maintainable architecture and code quality.
- Production stability and deployment safeguards.
- Explicit user approval for significant or irreversible actions.

Never merge, deploy, publish, delete important resources or make irreversible production changes without appropriate authorisation.

When cost efficiency conflicts with essential quality or safety requirements, prioritise the correct and reliable implementation.

## 9. Continuous Improvement

Periodically assess development workflows for unnecessary expense, duplicated effort, excessive context loading and recurring inefficiencies.

Proactively recommend improvements to project documentation, context management, model selection, tooling and development practices.

Adopt improvements that demonstrably reduce cost or repeated work without compromising development quality.

---

## Standing Instruction

**Apply these principles automatically whenever working on a software project.**

At the beginning of each task, establish relevant project context and existing progress.

During development, minimise unnecessary input, output, repeated reasoning and tool usage.

At meaningful checkpoints, preserve completed work and decisions in reusable project records.

When coordinating other AI development agents, ensure they follow the same guidelines.

**The overriding goal is to deliver correct, secure, maintainable and fully tested software with the least practical amount of wasted effort, time and expense.**
