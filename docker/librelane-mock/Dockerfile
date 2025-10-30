# Build ARG to allow overriding the base LibreLane image tag at build time
ARG BASE_IMAGE=ghcr.io/librelane/librelane:2.4.2
FROM ${BASE_IMAGE}

WORKDIR /opt/mock

# Ensure GNU make is available for EQY strategy orchestration
# The base image is Nix-based; install gnumake via nix and expose it on PATH.
RUN nix-env -iA nixpkgs.gnumake || true
ENV PATH=/root/.nix-profile/bin:${PATH}

# Add Pillow to enable image processing in the ApplyArt step
# Try Python 3.11 package name first, then generic python3Packages as fallback
RUN nix-env -iA nixpkgs.python311Packages.pillow || \
    nix-env -iA nixpkgs.python3Packages.pillow || true
# Ensure Python can import packages installed into the root nix profile
ENV PYTHONPATH=/root/.nix-profile/lib/python3.11/site-packages:${PYTHONPATH}

# Copy the mock plugin sources from the repository into the image
# NOTE: Build with the repository root as the build context, e.g.:
#   docker build -t my-librelane:mock -f docker/librelane-mock/Dockerfile .
COPY plugins/mock/librelane_plugin_mock /opt/mock/librelane_plugin_mock
COPY plugins/mock/pyproject.toml /opt/mock/pyproject.toml
COPY plugins/mock/apply_art.py /opt/mock/apply_art.py

# Make the plugin importable without pip (the base image lacks pip).
# LibreLane discovers plugins by importing modules named librelane_plugin_*
# that are importable via sys.path. Adding /opt/mock to PYTHONPATH is sufficient.
ENV PYTHONPATH=/opt/mock:${PYTHONPATH}

# Optionally, you can set env here for GUI robustness if needed later:
# ENV QT_X11_NO_MITSHM=1
