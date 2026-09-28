import os
import sys

# Make the backend root importable (app/, db/, brokers/, ...) when pytest
# runs from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
