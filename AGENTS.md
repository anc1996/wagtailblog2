# wagtailblog2 Agent Instructions

## Scope and Priority

Act as the full-stack collaborator for this repository. Follow this priority:

1. The user's current request and explicit authorization.
2. Data safety, production stability, and a viable rollback path.
3. The repository, live configuration, Git state, service state, and test evidence.
4. Project documentation and Git history.

Do not replace the existing architecture based on generic experience. Establish facts from code, configuration, logs, health checks, and service state before diagnosing or changing behavior.

## Project Facts To Verify

- Repository root: `F:\openclaw\workspace\wagtail\wagtailblog2`; Django package: `wagtailblog3`.
- Expected test environment: Conda `wagtailblog-test`; expected production environment: Conda `wagtailblog`.
- Expected production path: `/home/source/Django/wagtail/wagtailblog3`.
- Expected production stack: Nginx, uWSGI, systemd, MySQL, MongoDB, Redis, MinIO, Elasticsearch, Celery, and Filebeat.

These facts are starting points, not permission to assume that paths, service names, ports, commits, or deployment methods are current. Verify them for every deployment or service task.

## Required Workflow

For any substantial change, first establish the goal, scope, non-goals, acceptance criteria, data impact, deployment scope, and whether production is involved. Then inspect Git status, relevant code and tests, settings, URLs, Wagtail models, migrations, data flows, and affected services.

Before implementation, state the files to change and not change, storage and service impact, risks, test plan, rollback plan, and any decision requiring user confirmation. Make the smallest coherent change. Review the diff after each verifiable unit. Run proportionate checks and tests before claiming completion.

Only deploy an exact, tested commit. Do not deploy uncommitted working-tree state.

## Data and Production Safety

- Treat `BlogPage.body`, StreamField body data, MongoDB body documents, draft snapshots, revision pointers, and `mongo_content_id` as protected data.
- Keep Markdown as Markdown. Do not permanently replace it with HTML, and preserve the `markdown_block` storage key.
- Never run `flush`, destructive bulk repair, deletion, data restore, migration, publish, or real save without explicit authorization.
- Before a production data operation, state impact, backup requirement, execution order, and rollback plan, then obtain confirmation.
- Never put production credentials, tokens, passwords, keys, or private server details into source code, Git, logs, docs, or final responses.
- Do not use `git pull` or destructive/full-sync deployment commands against production unless its working-tree status and the operation are explicitly confirmed safe.

## MCP and Skill Routing

Use available capabilities deliberately. Check connection availability when a relevant MCP is needed; do not claim a tool was used if it was unavailable or failed.

- Django or Wagtail models, settings, migrations, views, StreamField, admin, or tests: use the Django/Wagtail development skill.
- Product features, plans, TDD, debugging, and verification: use the matching Superpowers workflow when available.
- Frontend templates, browser behavior, visual regressions, or responsive UI: use the frontend skills; use Playwright for real browser verification when visual behavior matters.
- Public documentation or long-form external pages: prefer `fetch_reader` from the `fetch` MCP to obtain article Markdown. Respect robots rules and do not access private hosts without explicit authorization.
- Database and cache inspection: prefer the Google Toolbox MCP's read-only tools when connected. Use it for MySQL schema/active-query/query-plan inspection, approved Mongo metadata reads, and Redis capacity/keyspace inspection. Never use it for writes unless the user explicitly authorizes a precise write operation.
- GitHub PRs, issues, Actions, and review work: use the GitHub or CodeRabbit skills/MCP when available. Do not push, create a PR, or change remote state without explicit authorization.
- CircleCI, Sentry, Render, Temporal, MagicPath, or Plugin Eval work: use the matching installed skill only when the task actually involves that platform or function.

MCP tools and skills are aids, not substitutes for repository evidence. Do not invoke unrelated tools merely because they are installed.

## Services and Deployment

`systemctl.md` is the service-maintenance baseline. For any changed service, queue, timer, scheduled job, Filebeat/indexing chain, dependency, environment variable, data directory, log directory, port, socket, uWSGI, Nginx, or reverse-proxy behavior, update `systemctl.md` in the same change.

For each added or changed service, document its name and responsibility, queue/port/socket, project and runtime paths, data and log paths, dependencies and startup order, enable/start/stop/restart commands, health check, retry behavior, rollback, and differences between test and production.

When service units change, run `systemctl daemon-reload` and verify the appropriate enablement and restart sequence. After a deployment or restart, verify failed units, the active/enabled status of the website, maintenance worker, Celery Beat, and Filebeat, plus socket/port reachability, website/admin access, Django checks, static assets, Redis worker connectivity, queue consumption, Beat scheduling, Filebeat/Elasticsearch health, logs, and task backlog.

## Completion Report

Report the completed scope, modified files, tested commit, tests and health checks run, whether `systemctl.md` changed, service changes, migrations or production data operations, rollback point, remaining test/production differences, and residual risks. State checks that could not run and why.
