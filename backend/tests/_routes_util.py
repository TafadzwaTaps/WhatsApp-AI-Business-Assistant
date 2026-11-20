"""Flatten FastAPI routes across versions: newer FastAPI wraps each included router in an internal
object (_IncludedRouter.original_router), older versions list the APIRoute objects directly."""


def iter_routes(routes):
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from iter_routes(inner.routes)
            continue
        yield r
        sub = getattr(r, "routes", None)
        if sub and type(r).__name__ == "Mount":
            yield from iter_routes(sub)


def find_route(app, path, method=None):
    for r in iter_routes(app.routes):
        if getattr(r, "path", "") == path and (method is None or method in (getattr(r, "methods", None) or ())):
            return r
    raise LookupError(f"route {method} {path} not found")
