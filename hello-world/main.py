#!/usr/bin/env python3
"""IAG5 python-script service: prints a Hello World JSON object to stdout."""
import json


def main():
    print(json.dumps({"success": True, "message": "Hello World from Gateway5"}))


if __name__ == "__main__":
    main()
