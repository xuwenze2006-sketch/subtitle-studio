"""Run all unit/integration tests with outbound sockets blocked by default."""
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


def blocked(*args, **kwargs):
    raise AssertionError('Outbound network is disabled in the offline test suite')


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    with patch.object(socket.socket,'connect',blocked), patch.object(socket.socket,'connect_ex',blocked), \
         patch.object(socket,'create_connection',blocked):
        suite=unittest.defaultTestLoader.discover(str(Path(__file__).parent))
        result=unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
