#!/usr/bin/env python3
"""Fake claude binary for testing capture_login.

When invoked as `claude auth login --claudeai`, writes a dummy .credentials.json
into $CLAUDE_CONFIG_DIR and exits 0.  No real claude binary, no network.
"""
import os
import json
import sys

cfg = os.environ["CLAUDE_CONFIG_DIR"]
os.makedirs(cfg, exist_ok=True)
json.dump(
    {
        "claudeAiOauth": {
            "accessToken": "fake-at",
            "refreshToken": "fake-rt",
            "expiresAt": 9999999999000,
            "subscriptionType": "max",
        }
    },
    open(os.path.join(cfg, ".credentials.json"), "w"),
)
sys.exit(0)
