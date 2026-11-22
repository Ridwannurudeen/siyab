"""Print the Siyab v1 measurement id from a six-field JSON file, without network reads."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from enclave.measure import measurement_id


def _measurement_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate measurement member")
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("measurement", type=Path, help="JSON object with exactly the six measurement fields")
    args = parser.parse_args(argv)
    try:
        measurement = json.loads(args.measurement.read_text(encoding="utf-8"),
                                 object_pairs_hook=_measurement_object)
        result = measurement_id(measurement)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(result)


if __name__ == "__main__":
    main()
