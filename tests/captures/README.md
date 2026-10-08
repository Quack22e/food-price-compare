Real platform pages captured with

    python -m server.inspect_page --save tests/captures <store URL>

are verified by `tests/test_real_captures.py`. This directory is empty in the
repository because no request to DoorDash, Uber Eats or Grubhub could be made
from the build environment (network egress policy). Do not put synthetic or
hand-edited pages here: only unmodified captures with their .json provenance.
