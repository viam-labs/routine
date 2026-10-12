#!/bin/bash
# Optional. The tests build these descriptors in Python so they do not need protoc.
# To regenerate grpclib stubs into tests/protos/gen:
#   python -m grpc_tools.protoc -I tests/protos --python_out=tests/protos/gen \
#     --grpclib_python_out=tests/protos/gen tests/protos/fake_apis.proto
set -euo pipefail
cd "$(dirname "$0")/../.."
python -m grpc_tools.protoc -I tests/protos \
  --python_out=tests/protos/gen \
  --grpclib_python_out=tests/protos/gen \
  tests/protos/fake_apis.proto
echo "Wrote tests/protos/gen"
