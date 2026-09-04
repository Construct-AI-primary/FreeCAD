# =============================================================================
# Dockerfile – FreeCAD Headless Web Service for Render
# =============================================================================
# Builds a container with FreeCAD (headless) + FastAPI wrapper so a
# third-party application can call FreeCAD's Python API over HTTP.
# =============================================================================

FROM condaforge/miniforge3:23.3.1-1 AS builder

# Install FreeCAD + runtime Python dependencies from conda-forge
RUN conda install -c conda-forge \
        freecad \
        python=3.11 \
        fastapi \
        uvicorn \
        python-multipart \
        -y \
    && conda clean -afy \
    && rm -rf /opt/conda/pkgs/*

# ── Final image ──────────────────────────────────────────────────────────
FROM condaforge/miniforge3:23.3.1-1

COPY --from=builder /opt/conda /opt/conda

# Environment: headless mode, no display needed for FreeCADCmd
ENV FREECAD_DISABLE_GUI=1
ENV DISPLAY=:99
ENV PYTHONIOENCODING=utf-8

# Ensure conda environment is active for all RUN / CMD
ENV PATH /opt/conda/bin:$PATH
ENV CONDA_DEFAULT_ENV=base

WORKDIR /app

# Copy the API wrapper
COPY main.py .

# Health check (Render requires this)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]