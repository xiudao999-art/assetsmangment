from contextlib import nullcontext

from app.infrastructure.pg_material_submission_repo import PgMaterialSubmissionRepo


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _CaptureConnection:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = []

    def execute(self, sql, params=()):
        self.sql = sql
        self.params = list(params)
        return _Result(self.rows)


def _repo_with_connection(connection):
    repo = object.__new__(PgMaterialSubmissionRepo)
    repo._table = "material_submission"
    repo._permission_table = "material_submission_permission"
    repo._operation_table = "material_submission_operation"
    repo._conn = lambda: nullcontext(connection)
    return repo


def test_submission_list_left_joins_user_names_and_permission():
    row = (
        101, "team", "", "drama", "video.mp4", "", 0, "video.mp4",
        "title", "1-2", "", 1, "", "", "", 1, "", [],
        "creator", None, "updater", None, 0, 0,
        "Creator Name", "Updater Name", "read_edit",
    )
    connection = _CaptureConnection([row])
    repo = _repo_with_connection(connection)

    items = repo.list(
        visible_to_user_id="viewer", sort_by="created_time", sort_order="desc",
        limit=10,
    )

    assert "LEFT JOIN app_user created_user" in connection.sql
    assert "LEFT JOIN app_user updated_user" in connection.sql
    assert "LEFT JOIN material_submission_permission visible_permission" in connection.sql
    assert connection.params == ["viewer"]
    assert items[0]._created_by_name_hint == "Creator Name"
    assert items[0]._updated_by_name_hint == "Updater Name"
    assert items[0]._permission_type_hint == "read_edit"


def test_creator_accounts_are_aggregated_in_one_query():
    connection = _CaptureConnection([("u1", "Alice", 123), ("u2", "u2", 100)])
    repo = _repo_with_connection(connection)

    items = repo.list_creator_accounts(visible_to_user_id="viewer")

    assert "GROUP BY submission.create_by" in connection.sql
    assert "LEFT JOIN app_user creator" in connection.sql
    assert connection.params == ["viewer"]
    assert items == [{"id": "u1", "name": "Alice"}, {"id": "u2", "name": "u2"}]


def test_postgres_pool_is_shared_by_dsn(monkeypatch):
    import psycopg_pool
    from app.infrastructure import pg_pool

    created = []

    class FakePool:
        check_connection = staticmethod(lambda connection: None)

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            created.append(self)

        def connection(self):
            return nullcontext(object())

        def close(self):
            self.closed = True

    pg_pool.close_all_pools()
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", FakePool)

    first = pg_pool.get_pool("postgresql://example/test")
    second = pg_pool.get_pool("postgresql://example/test")

    assert first is second
    assert len(created) == 1
    assert created[0].kwargs["max_size"] >= created[0].kwargs["min_size"]
    pg_pool.close_all_pools()
    assert first.closed is True
