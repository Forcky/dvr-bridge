"""DVR Bridge: a modern browser interface for older RaySharp-family DVRs."""
import os

__version__ = "0.1.0"
# Set by the image build (Dockerfile ARG REVISION); "unknown" when run from a checkout.
REVISION = os.environ.get("DVRBRIDGE_BUILD_REVISION", "unknown")
