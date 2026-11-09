"""Tests never see the machine's real configuration.

common.config reads %LOCALAPPDATA%\\Gateway\\config.json, and that file may name a real library
with its own folder names. Point LOCALAPPDATA at an empty temporary folder before any test runs,
so every test starts from the English defaults; tests that need a config write one there.
"""
import os
import tempfile

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="gateway-tests-appdata-")
