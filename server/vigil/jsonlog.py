"""One JSON object per log line.

Text logs are pleasant at a terminal and useless to anything else: a
multi-line traceback or a message with a newline in it becomes several
physical lines, and the request id is a column a human reads. JSON hands the
stream to Loki/ELK/CloudWatch with no parser to write, and ``json.dumps``
escapes newlines so a record is always exactly one line.
"""

import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record):
        obj = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str)
