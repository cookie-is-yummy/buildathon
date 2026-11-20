"""Entry point. Importing selector as a module lets Python use its precompiled bytecode."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import selector  # noqa: E402

selector.main()
