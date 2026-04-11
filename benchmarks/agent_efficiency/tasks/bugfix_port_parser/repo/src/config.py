def parse_port(value):
    try:
        return int(value)
    except ValueError:
        return 0
