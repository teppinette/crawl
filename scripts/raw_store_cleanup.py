#!/usr/bin/env python3
"""Daily cleanup of raw response files older than 90 days."""
import sys
sys.path.insert(0, "/home/copapadmin/crawl/api")

import raw_store

if __name__ == "__main__":
    result = raw_store.cleanup()
    print(f"Raw store cleanup: {result}")
