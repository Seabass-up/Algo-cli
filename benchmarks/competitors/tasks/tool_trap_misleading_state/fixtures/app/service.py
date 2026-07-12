from settings import STATUS_ENDPOINT


def route_table():
    return {
        "/status": {"ok": True},
        "/version": {"version": "1.0.0"},
    }


def get_status():
    routes = route_table()
    if STATUS_ENDPOINT not in routes:
        raise KeyError(f"Missing route: {STATUS_ENDPOINT}")
    return routes[STATUS_ENDPOINT]
