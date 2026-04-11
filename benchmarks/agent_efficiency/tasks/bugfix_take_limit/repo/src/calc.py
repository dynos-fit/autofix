def take_limit(items, limit):
    if limit <= 0:
        return []
    return items[: limit - 1]
