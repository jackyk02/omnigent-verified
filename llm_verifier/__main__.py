"""Preview a criteria file exactly as the verifier will see it:

python -m llm_verifier my_criteria.md
python -m llm_verifier swe_bench
"""

import sys

from llm_verifier.prompts import _main

if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
