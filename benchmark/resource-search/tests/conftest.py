import os
import sys

# evaluate_resource_search.py and build_ground_truth.py live one directory up
# and are deployed/run as bare scripts (not a package), so make them
# importable no matter which directory pytest is invoked from -- same
# pattern as agents/b2ai-copilot/lambda/b2aiSqlRag/tests/conftest.py.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
