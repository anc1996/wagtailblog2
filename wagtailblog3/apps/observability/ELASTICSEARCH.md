# Elasticsearch log read model

The file logs remain the audit fallback. Elasticsearch is an optional read
model and is disabled unless `ELASTICSEARCH_LOG_ENABLED=1` is set.

The log index is isolated from Wagtail content search:

```text
wagtailblog-test-logs-000001
wagtailblog-test-logs-read
wagtailblog-test-logs-write
```

Production must use a different prefix. Do not reuse `WAGTAILSEARCH_BACKENDS`
or the Wagtail content index prefix.

## Configuration

Set these values in the environment, not in source control:

```text
ELASTICSEARCH_LOG_ENABLED=1
ELASTICSEARCH_LOG_URL=http://192.168.20.2:9200
ELASTICSEARCH_LOG_READ_INDEX=wagtailblog-test-logs-read
ELASTICSEARCH_LOG_WRITE_INDEX=wagtailblog-test-logs-write
ELASTICSEARCH_LOG_INGEST_PIPELINE=wagtailblog-test-logs-normalize-v2
ELASTICSEARCH_LOG_REPLICAS=0
```

Use HTTPS, certificate verification, and a least-privilege API key in
production. The single-node test cluster should use zero replicas. A
multi-node production cluster should use at least one replica and snapshots.
`ELASTICSEARCH_LOG_VERIFY_CERTS` defaults to `true`; only turn it off for an
isolated test endpoint that has no TLS certificate.

When Elasticsearch cannot answer a request, each Django worker opens a
five-second local circuit breaker before falling back to bounded file reads.
Set `ELASTICSEARCH_LOG_FAILURE_COOLDOWN` only when a different retry interval
is required. This prevents a failed optional backend from adding its network
timeout to every admin page load.

## Preparing the index

The command is intentionally confirmation-gated. It only creates the first
index and aliases; it never deletes or flushes data. If the physical index
already exists after an interrupted setup, it safely adds only missing aliases.
It refuses to repoint a write alias that already belongs to another index:

```bash
python manage.py prepare_log_index --confirm
```

It also creates the configured ingest pipeline only when that pipeline does
not already exist. Operators retain ownership of ILM policies, snapshots, and
future rollover actions; set `ELASTICSEARCH_LOG_ILM_POLICY` only after the
policy has been created outside Django.

For a one-time test import of the existing registered files, use the bounded
bootstrap command. It writes only after `--confirm` and caps the record count:

```bash
python manage.py index_log_files --confirm --max-records 10000
```

This command is for migration or testing. Continuous production ingestion
should use Filebeat or Elastic Agent so request workers never wait for ES.

For Filebeat, render a config from the registered catalog. The command prints
only environment-variable references for credentials and never writes a file:

```bash
python manage.py render_filebeat_config > /etc/filebeat/wagtailblog-logs.yml
```

The test WSL service template is kept at
`ops/filebeat/wagtailblog-test.service`; install it only after the generated
config has passed `filebeat test config` and `filebeat test output`.

Set `ELASTICSEARCH_LOG_URL` (and the API key or username/password variables)
in the Filebeat service environment. The generated inputs use multiline
framing, add the registered domain/kind fields, fingerprint each event for a
stable sort key, map the same fingerprint to the Elasticsearch document ID for
idempotent retries, and send events through the versioned ingest pipeline. The
rendered Filebeat 8.x config places the pipeline ID in
`output.elasticsearch.parameters.pipeline`, which ensures it is sent on every
bulk request.

Set `ELASTICSEARCH_LOG_AUTH_MODE=api_key` or `basic` in the Django command
environment when generating the Filebeat config. This is non-sensitive and
lets the rendered YAML reference Filebeat's own credential environment
variables without exposing a credential to the command output.

Log ingestion is not performed by an admin request. Use Filebeat or Elastic
Agent to ship the registered files to the write alias. Keep ingestion and
index lifecycle configuration outside Django request handling.

## Cleanup synchronization

Admin cleanup keeps the existing file semantics: active files are truncated
in place and registered rotated files are unlinked. When the ES read model is
enabled, each successful file result creates a durable `LogIndexSyncJob`.
The worker runs a registry-bounded delete-by-query and repeats it after a short
delay so events already buffered by Filebeat cannot reappear permanently.

The v2 pipeline preserves Filebeat's millisecond `observed_at` timestamp and
promotes `log.file.device_id`, `log.file.inode`, and `log.offset` to indexed
source identity fields. Run `prepare_log_index
--confirm` after selecting the v2 pipeline; the command adds only missing
mapping properties and never deletes documents. Re-render and validate the
Filebeat configuration before restarting the test shipper.

File cleanup remains successful when ES is unavailable. The audit page shows
the independent index state, while Celery Beat dispatches due outbox jobs every
30 seconds. A permanent error enters `dead_letter`; after correcting the ES
mapping or credentials, retry it explicitly:

```bash
python manage.py retry_log_index_sync AUDIT_ID --confirm
```

Use a separate Django API key with delete-by-query permission. The Filebeat API
key should retain ingest-only privileges.

When the ES read model is unavailable, the admin records page automatically
falls back to the bounded local reader. The fallback is deliberately limited
by the existing cursor and byte-budget controls.
