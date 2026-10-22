"""A JSON serializer for the Redis cache.

Django's default Redis serializer pickles, and unpickling runs code: anyone who
could write to Redis could run it in every web worker. Everything Vigil caches
is plain data (ids, token strings, template lists), so JSON carries all of it
and reading a value back can only ever produce data.
"""

import json


class JSONSerializer:
    def dumps(self, obj):
        # Integers stay raw, as Django's own serializer leaves them, so
        # incr()/decr() keep working on the server side.
        if type(obj) is int:
            return obj
        return json.dumps(obj, separators=(",", ":")).encode("utf-8")

    def loads(self, data):
        try:
            return int(data)
        except ValueError:
            return json.loads(data)
