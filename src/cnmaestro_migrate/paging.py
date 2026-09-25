"""limit/offset paging shared by both UI APIs."""
PAGE = 100


def paged(call, path, key, page=PAGE):
    """Read every row of a paged list. call(method, path) returns the decoded JSON body."""
    rows, offset = [], 0
    while True:
        sep = "&" if "?" in path else "?"
        batch = call("GET", f"{path}{sep}limit={page}&offset={offset}").get("data", {}).get(key) or []
        rows += batch
        if len(batch) < page:
            return rows
        offset += page
