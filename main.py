# =============================================================================
# FreeCAD Headless Web Service – FastAPI Wrapper
# =============================================================================
# Exposes a REST API so third-party applications can use FreeCAD's
# Python API for CAD operations (conversion, scripting, etc.).
#
# Run locally:
#   uvicorn main:app --reload --port 8000
#
# Run via Docker:
#   docker build -t freecad-service . && docker run -p 8000:8000 freecad-service
# =============================================================================

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
# ---------------------------------------------------------------------------
# FreeCAD initialisation (headless / console mode)
# ---------------------------------------------------------------------------
# We use FreeCADCmd – the console-only executable – to execute short Python
# snippets sent by the API.  This keeps each request isolated and avoids the
# fragility of embedding the C++ FreeCAD library directly in the Python
# server process.
# ---------------------------------------------------------------------------

FREECADCMD: str | None = None


def _locate_freecadcmd() -> str | None:
    """Return the absolute path to ``FreeCADCmd``, or ``None``."""
    # 1. PATH lookup (conda installs FreeCADCmd here)
    for p in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(p) / "FreeCADCmd"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())

    # 2. Common conda locations
    for prefix in ("/opt/conda", os.path.expanduser("~/miniforge3"),
                   os.path.expanduser("~/miniconda3")):
        for loc in (
            Path(prefix) / "bin" / "FreeCADCmd",
            Path(prefix) / "envs" / "freecad" / "bin" / "FreeCADCmd",
        ):
            if loc.is_file() and os.access(loc, os.X_OK):
                return str(loc.resolve())

    # 3. FreeCAD installed from system package
    for sysbin in ("/usr/bin/FreeCADCmd", "/usr/local/bin/FreeCADCmd"):
        p = Path(sysbin)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve())

    return None
# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="FreeCAD Headless API",
    version="26.3.0",
    description="REST API wrapping the FreeCAD parametric modeller in headless mode.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ScriptRequest(BaseModel):
    """Payload for ``POST /api/run-script``."""
    script: str


class ScriptResponse(BaseModel):
    """Response from a script execution."""
    success: bool
    stdout: str = ""
    stderr: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _freecadcmd_or_raise() -> str:
    global FREECADCMD
    if FREECADCMD is None:
        FREECADCMD = _locate_freecadcmd()
    if FREECADCMD is None:
        raise HTTPException(
            status_code=503,
            detail="FreeCADCmd executable not found. Check the container build.",
        )
    return FREECADCMD


def _run_freecad_script(script: str, *, timeout: int = 120) -> ScriptResponse:
    """
    Execute a Python *script* in a subprocess via ``FreeCADCmd -c``.

    The script is written to a temporary file so that error line-numbers are
    meaningful.
    """
    cmd = _freecadcmd_or_raise()
    script = textwrap.dedent(script).strip()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script)
        tmp_path = f.name

    try:
        proc = subprocess.run(
            [cmd, "-c", tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "FREECAD_DISABLE_GUI": "1"},
        )
        return ScriptResponse(
            success=proc.returncode == 0,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    except subprocess.TimeoutExpired:
        return ScriptResponse(success=False,
                              stderr=f"Script timed out after {timeout}s.")
    except FileNotFoundError:
        raise HTTPException(status_code=503,
                            detail="FreeCADCmd not found at runtime.")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _run_freecad_script_get_json(script: str, *,
                                  timeout: int = 120) -> Any:
    """
    Execute a FreeCAD script and expect the last printed line to be valid JSON.
    """
    result = _run_freecad_script(script, timeout=timeout)
    if not result.success:
        raise HTTPException(
            status_code=422,
            detail={"stdout": result.stdout, "stderr": result.stderr},
        )
    lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
    if not lines:
        raise HTTPException(
            status_code=500,
            detail="Script produced no output (expected JSON on stdout).",
        )
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=500,
            detail=f"Last output line is not valid JSON: {lines[-1]}",
        )


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Render health-check endpoint."""
    cad = _freecadcmd_or_raise()
    try:
        info = _run_freecad_script_get_json(
            "import FreeCAD, json; print(json.dumps(dict(version=FreeCAD.Version)))",
            timeout=10,
        )
        return {
            "status": "healthy",
            "freecad": cad,
            "version": info.get("version"),
        }
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "detail": str(exc)},
        )


@app.get("/api/info")
def info():
    """Return FreeCAD version and configuration."""
    return _run_freecad_script_get_json(
        """
        import FreeCAD, json

        info = {
            "version":        FreeCAD.Version,
            "build_revision": getattr(FreeCAD, "BuildRevision", "unknown"),
            "compiler":       getattr(FreeCAD, "Compiler", "unknown"),
            "platform":       getattr(FreeCAD, "Platform", "unknown"),
            "python_version": FreeCAD.sysVersion(),
        }
        print(json.dumps(info))
        """,
    )


@app.post("/api/run-script", response_model=ScriptResponse)
def run_script(req: ScriptRequest):
    """
    Execute an arbitrary FreeCAD Python script (macro).

    The script runs in a *fresh* FreeCAD process every time, so state is
    never shared between requests.
    """
    return _run_freecad_script(req.script)
@app.post("/api/convert")
async def convert(
    file: UploadFile = File(...),
    target_format: str = Form(...),
):
    """
    Upload a CAD file and convert it to *target_format*.

    Supported formats (input -> output):
      STEP (.step/.stp)  ->  STL, FCStd, IGES, OBJ, BREP
      IGES (.iges/.igs)  ->  STEP, STL, FCStd, BREP
      STL   (.stl)       ->  STEP, FCStd, BREP
      OBJ   (.obj)       ->  STEP, STL, FCStd
      BREP  (.brep)      ->  STEP, STL, IGES, FCStd
      FCStd (.fcstd)     ->  STEP, STL, IGES, OBJ, BREP

    Maximum file size: 100 MB.
    """
    MAX_SIZE = 100 * 1024 * 1024  # 100 MB
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 100 MB).")

    ext = Path(file.filename or "upload").suffix.lower()
    input_format = _extension_to_import(ext)

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
        f.write(content)
        input_path = f.name

    output_suffix = _format_to_extension(target_format.lower())

    with tempfile.NamedTemporaryFile(suffix=output_suffix, delete=False) as f:
        output_path = f.name

    script = textwrap.dedent(f"""
        import FreeCAD, json

        try:
            doc = FreeCAD.open("{input_path}")
        except Exception as exc:
            print(json.dumps({{"success": False, "error": str(exc)}}))
            exit(1)

        try:
            if "{target_format}" == "stl":
                import Mesh
                Mesh.export([doc.Objects], "{output_path}")
            elif "{target_format}" in ("step", "iges"):
                import ImportPart
                ImportPart.export(doc.Objects, "{output_path}")
            elif "{target_format}" == "obj":
                import Mesh
                mesh = Mesh.Mesh()
                for obj in doc.Objects:
                    if hasattr(obj, "Shape") and obj.Shape:
                        mesh.addMesh(obj.Shape.tessellate(0.1))
                mesh.writeOBJ("{output_path}")
            elif "{target_format}" == "brep":
                doc.Objects[0].Shape.exportBrep("{output_path}")
            elif "{target_format}" == "fcstd":
                doc.saveAs("{output_path}")
            else:
                print(json.dumps({{"success": False,
                                   "error": "Unsupported target format"}}))
                exit(1)

            FreeCAD.closeDocument(doc.Name)
            print(json.dumps({{"success": True, "output": "{output_path}"}}))
        except Exception as exc:
            FreeCAD.closeDocument(doc.Name)
            print(json.dumps({{"success": False, "error": str(exc)}}))
            exit(1)
    """)

    result = _run_freecad_script(script)
    Path(input_path).unlink(missing_ok=True)

    data = json.loads(
        result.stdout.strip().split("\n")[-1]
    ) if result.stdout.strip() else {}

    if not data.get("success"):
        return JSONResponse(
            status_code=422,
            content={"error": data.get("error", "Conversion failed"),
                       "stderr": result.stderr},
        )

    output_file = data["output"]
    if not Path(output_file).is_file():
        return JSONResponse(
            status_code=500,
            content={"error": "Output file not found."},
        )

    with open(output_file, "rb") as f:
        payload = f.read()
    Path(output_file).unlink(missing_ok=True)

    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition":
                f'attachment; filename="converted{output_suffix}"',
        },
    )
# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

_FORMAT_MAP: dict[str, str] = {
    ".step": "step",
    ".stp": "step",
    ".iges": "iges",
    ".igs": "iges",
    ".stl": "stl",
    ".obj": "obj",
    ".brep": "brep",
    ".fcstd": "fcstd",
}

_REVERSE_FORMAT_MAP: dict[str, str] = {
    "step": ".step",
    "iges": ".igs",
    "stl": ".stl",
    "obj": ".obj",
    "brep": ".brep",
    "fcstd": ".fcstd",
}


def _extension_to_import(ext: str) -> str:
    fmt = _FORMAT_MAP.get(ext)
    if fmt is None:
        raise HTTPException(status_code=400,
                            detail=f"Unsupported input extension: {ext}")
    return fmt


def _format_to_extension(fmt: str) -> str:
    ext = _REVERSE_FORMAT_MAP.get(fmt)
    if ext is None:
        raise HTTPException(status_code=400,
                            detail=f"Unsupported target format: {fmt}")
    return ext


# ---------------------------------------------------------------------------
# Startup event – verify FreeCAD is reachable
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    """Warm up: make sure FreeCADCmd is available on first request."""
    _freecadcmd_or_raise()


# ---------------------------------------------------------------------------
# Entrypoint (for debugging / direct run)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)