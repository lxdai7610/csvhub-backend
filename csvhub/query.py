import base64
from contextlib import closing
import hashlib
import hmac
import json
import sqlite3
import threading
import time

from .cache import ResultCache
from .parsing import normalize


class QueryError(ValueError):
    pass


class QueryTimeout(Exception):
    pass


def fingerprint(dataset_id, filters):
    shape = json.dumps({"dataset": dataset_id, "filters": filters}, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(shape.encode()).hexdigest()


def prefix_upper(prefix):
    for position in range(len(prefix) - 1, -1, -1):
        point = ord(prefix[position])
        if point < 0x10FFFF:
            successor = point + 1
            if 0xD800 <= successor <= 0xDFFF:
                successor = 0xE000
            return prefix[:position] + chr(successor)
    return None


class QueryService:
    def __init__(self, settings, catalog):
        self.settings, self.catalog = settings, catalog
        self.cache = ResultCache(settings.result_cache_bytes, settings.cache_ttl)
        self.secret = settings.token().encode()
        self.slots = threading.BoundedSemaphore(settings.read_concurrency)

    def _encode_cursor(self, payload):
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
        signature = hmac.new(self.secret, raw, hashlib.sha256).hexdigest().encode()
        return base64.urlsafe_b64encode(signature + b"." + raw).decode()

    def _decode_cursor(self, value):
        try:
            signed = base64.b64decode(value.encode(), altchars=b"-_", validate=True)
            signature, raw = signed.split(b".", 1)
            expected = hmac.new(self.secret, raw, hashlib.sha256).hexdigest().encode()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, UnicodeError, TypeError) as error:
            raise QueryError("Invalid cursor") from error

    def with_annotations(self, dataset_id, request, response, annotations):
        # Public rows may be cached across readers. Personal, mutable annotations must stay outside that cache.
        result = {**response, "annotations": annotations}

        def encoded_size(value):
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        if encoded_size(result) <= self.settings.max_response_bytes:
            return result

        # If adding notes exceeds the response budget, shorten the page and rebuild its cursor.
        # Reusing the raw page's cursor here would silently skip the records that no longer fit.
        base = {**response, "rows": [], "annotations": {}, "next_cursor": None}
        used, kept, kept_annotations, cursor = encoded_size(base), [], {}, None
        query_fingerprint = fingerprint(dataset_id, [c.model_dump() for c in request.filters])
        anchor = request.filters[0].field if request.filters else None
        for index, row in enumerate(response["rows"]):
            row_key = str(row["_row"])
            mark = annotations[row_key]
            addition = encoded_size(row) + encoded_size(row_key) + 1 + encoded_size(mark) + (2 if kept else 0)
            next_cursor = response["next_cursor"] if index == len(response["rows"]) - 1 else self._encode_cursor({
                "version": response["version"], "query": query_fingerprint, "row": row["_row"],
                "value": row[anchor] if anchor else None})
            if used + addition + encoded_size(next_cursor) - 4 > self.settings.max_response_bytes:
                break
            used += addition
            kept.append(row)
            kept_annotations[row_key] = mark
            cursor = next_cursor
        if not kept:
            raise QueryError("Record exceeds interactive response size limit")
        return {**response, "rows": kept, "annotations": kept_annotations, "next_cursor": cursor}

    def query(self, dataset_id, request):
        dataset = self.catalog.get("datasets", dataset_id)
        if dataset is None:
            raise QueryError("Dataset not found")
        version = dataset["active_version"]
        if version is None:
            raise QueryError("Dataset has no published version")
        columns = dataset["columns"]
        field_map = {c["name"]: (i, c) for i, c in enumerate(columns)}
        conditions, params = [], []
        anchor = None
        for condition in request.filters:
            if condition.field not in field_map:
                raise QueryError("Unknown filter column")
            index, column = field_map[condition.field]
            try:
                value = normalize(str(condition.value), column)
            except (ValueError, OverflowError) as error:
                raise QueryError("Invalid filter value for configured column type") from error
            if value is None:
                raise QueryError("Empty filter values are not supported")
            sql_column = f"c{index}"
            if condition.op == "prefix":
                if column["kind"] != "text":
                    raise QueryError("Prefix search requires a text column")
                conditions.append(f"{sql_column} >= ?")
                params.append(value)
                upper = prefix_upper(value)
                if upper is not None:
                    conditions.append(f"{sql_column} < ?")
                    params.append(upper)
            else:
                operator = {"eq": "=", "gte": ">=", "lte": "<="}[condition.op]
                conditions.append(f"{sql_column} {operator} ?")
                params.append(value)
            if anchor is None:
                anchor = index
        query_fingerprint = fingerprint(dataset_id, [c.model_dump() for c in request.filters])
        if request.cursor:
            cursor = self._decode_cursor(request.cursor)
            if cursor.get("version") != version:
                raise QueryError("Cursor belongs to an older dataset version; restart query")
            if cursor.get("query") != query_fingerprint:
                raise QueryError("Cursor belongs to a different query")
            row = cursor.get("row")
            if not isinstance(row, int) or not 0 <= row < 2**63:
                raise QueryError("Invalid cursor position")
            if anchor is None:
                conditions.append("row_no > ?")
                params.append(row)
            else:
                conditions.append(f"(c{anchor},row_no) > (?,?)")
                params.extend((cursor.get("value"), row))
        order = "row_no" if anchor is None else f"c{anchor},row_no"
        # Every imported field is queryable. Only force an index that actually exists;
        # otherwise SQLite can use another condition's index, or a time-bounded scan.
        forced = f" INDEXED BY ix_{anchor}" if anchor is not None and columns[anchor]["indexed"] else ""
        sql = "SELECT * FROM records" + forced
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += f" ORDER BY {order} LIMIT ?"
        params.append(request.limit + 1)
        key = json.dumps({"scope": "published_data", "version": version, "request": request.model_dump()}, sort_keys=True)
        cached = self.cache.get(key) if not request.explain else None
        if cached is not None:
            cached["cache_hit"] = True
            return cached
        if not self.slots.acquire(timeout=self.settings.query_timeout):
            raise QueryTimeout("Query capacity is busy; retry later")
        try:
            path = self.settings.data_dir / "versions" / f"{version}.sqlite"
            uri = path.as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
                db.execute("PRAGMA query_only=ON")
                db.execute(f"PRAGMA cache_size=-{self.settings.sqlite_cache_kib}")
                db.execute("PRAGMA mmap_size=0")
                deadline = time.monotonic() + self.settings.query_timeout
                db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params)] if request.explain else None
                try:
                    if not request.filters and not request.cursor and request.limit == 50 and not request.explain:
                        warm = db.execute("SELECT payload FROM warmup WHERE id=1").fetchone()
                        if warm:
                            response = json.loads(warm[0])
                            self.cache.put(key, response)
                            return response
                    rows, response_bytes, more = [], 0, False
                    for row in db.execute(sql, params):
                        row_bytes = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
                        if len(rows) >= request.limit or response_bytes + row_bytes > self.settings.max_response_bytes:
                            if not rows:
                                raise QueryError("Record exceeds interactive response size limit")
                            more = True
                            break
                        rows.append(row)
                        response_bytes += row_bytes
                except sqlite3.OperationalError as error:
                    if "interrupt" in str(error).lower():
                        raise QueryTimeout("Query exceeded time budget; use more selective filters") from error
                    raise
        finally:
            self.slots.release()
        results = [{"_row": row[0], **{column["name"]: row[i + 1] for i, column in enumerate(columns)}} for row in rows]
        next_cursor = None
        if more and rows:
            last = rows[-1]
            next_cursor = self._encode_cursor({"version": version, "query": query_fingerprint,
                                              "row": last[0], "value": None if anchor is None else last[anchor + 1]})
        response = {"version": version, "rows": results, "next_cursor": next_cursor, "cache_hit": False}
        if plan is not None:
            response["plan"] = plan
        else:
            self.cache.put(key, response)
        return response

