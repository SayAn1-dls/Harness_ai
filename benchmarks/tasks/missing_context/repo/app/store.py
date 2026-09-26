class UserStore:
    def __init__(self):
        self._rows = {}

    def exists(self, key):
        return key in self._rows

    def add(self, key, row):
        self._rows[key] = row

    def count(self):
        return len(self._rows)
