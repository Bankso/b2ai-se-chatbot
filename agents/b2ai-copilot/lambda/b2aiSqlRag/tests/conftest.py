import os
import sys

# lambda_function.py is deployed as a bare module (not a package), so make it
# importable no matter which directory pytest is invoked from.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
