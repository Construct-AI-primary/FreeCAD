# =============================================================================
# Dockerfile – FreeCAD Headless Web Service for Render
# =============================================================================
# Builds a container with FreeCAD (headless) + FastAPI wrapper so a
# third-party application can call FreeCAD's Python API over HTTP.
#
# Uses mamba (10-50x faster than conda) for the massive FreeCAD dep graph,
# then pip for lightweight Python web deps — avoids re-solving FreeCAD's
# enormous dependency tree every time.
# =============================================================================

FROM condaforge/miniforge3:latest AS builder

# ── Install FreeCAD (headless) via mamba ───────────────────────────────
# mamba solves conda's dependency graph 10-50x faster than conda.
# FreeCAD pulls in ~200+ packages (Qt, OCC, Coin3D, VTK, etc.).
RUN mamba install -c conda-forge \
        freecad \
        python=3.11 \
        -y \
    && mamba clean -afy \
    && rm -rf /opt/conda/pkgs/*

# ── Install web dependencies via pip ───────────────────────────────────
# Separate step avoids re-solving FreeCAD's enormous dependency graph.
RUN pip install --no-cache-dir \
        fastapi==0.115.0 \
        uvicorn==0.29.0 \
        python-multipart==0.0.9

# ── Final image ────────────────────────────────────────────────────────
FROM condaforge/miniforge3:latest

COPY --from=builder /opt/conda /opt/conda

# Headless mode – no display or GUI libraries needed
ENV FREECAD_DISABLE_GUI=1
ENV PYTHONIOENCODING=utf-8
ENV PATH=/opt/conda/bin:$PATH
ENV LD_LIBRARY_PATH=/opt/conda/lib

WORKDIR /app

COPY main.py .

# Render health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]