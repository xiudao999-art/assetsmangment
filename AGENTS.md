# Repository instructions

## Packaging

When the user asks to “打包” or requests a deployment package:

1. Run the repository-provided `deploy\pack.cmd` from the repository root.
2. Treat `D:\project\assetsmangment\assetsmangment.zip` as the only deployment artifact to deliver.
3. The script must overwrite the existing root-level `assetsmangment.zip`.
4. Do not substitute or primarily deliver `release\assetsmangment.zip`, a timestamped archive, or a manually assembled archive.
5. After packaging, verify the root archive contains `Dockerfile`, `app/`, and `frontend/`, excludes `__pycache__` and `*.pyc`, and report its SHA256 hash.

## Persistence and PostgreSQL

1. Local-file storage and JSON repository implementations are legacy and are no longer maintained for business runtime behavior. Do not add features, fixes, or new business persistence paths to JSON/local repositories unless the user explicitly requests legacy work.
2. PostgreSQL is the maintained business persistence implementation and the default target for repository changes.
3. Every PostgreSQL repository and database operation must use the shared connection-pool infrastructure in `app/infrastructure/pg_pool.py`. Do not create a new direct `psycopg.connect()` connection per operation.
4. Never execute SQL once per row/item inside a business loop. Avoid N+1 queries. Use set-based SQL such as `JOIN`/`LEFT JOIN`, `IN`/`ANY`, CTEs, batch writes (`executemany` where appropriate), or database aggregation.
5. For list APIs, keep the number of SQL round trips bounded and independent of the number of returned rows. Review query plans and indexes when performance work explicitly permits index changes.
6. Redis clients that use the same Redis URL must share a process-wide connection pool, use a configured `max_connections` limit, and close pools during application shutdown. Do not create a Redis client or pool per business operation.
