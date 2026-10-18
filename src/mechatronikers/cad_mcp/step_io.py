"""STEP export with an explicit application protocol, and re-import (docs/05)."""

import re
from pathlib import Path
from typing import Any, Literal

import build123d as bd
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.Interface import Interface_Static
from OCP.Message import Message, Message_Gravity
from OCP.STEPCAFControl import STEPCAFControl_Controller, STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_Controller, STEPControl_Reader, STEPControl_StepModelType
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.XSControl import XSControl_WorkSession

StepSchema = Literal["AP214", "AP242"]

_SCHEMA_VALUES: dict[StepSchema, str] = {"AP214": "AP214IS", "AP242": "AP242DIS"}


def _quiet() -> None:
    """Only let OpenCascade print failures; the writer is chatty on stdout otherwise."""
    for printer in Message.DefaultMessenger_s().Printers():
        printer.SetTraceLevel(Message_Gravity.Message_Fail)


class StepExportError(RuntimeError):
    pass


class StepImportError(RuntimeError):
    pass


def export_step(
    shape: bd.Shape[Any], path: str | Path, schema: StepSchema = "AP242", name: str | None = None
) -> Path:
    """Write `shape` as STEP (mm) using the given schema, via the XDE writer (names kept)."""
    path = Path(path)
    _quiet()
    # Controller init resets the write statics, so the schema must be set afterwards and
    # before the writer (and its model) is created.
    STEPCAFControl_Controller.Init_s()
    STEPControl_Controller.Init_s()
    Interface_Static.SetCVal_s("write.step.schema", _SCHEMA_VALUES[schema])
    Interface_Static.SetCVal_s("write.step.unit", "MM")

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    label = shape_tool.AddShape(shape.wrapped, False)
    if name or shape.label:
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name or shape.label))

    writer = STEPCAFControl_Writer(XSControl_WorkSession(), False)
    writer.SetNameMode(True)
    writer.SetColorMode(True)
    writer.SetLayerMode(True)
    if not writer.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs):
        raise StepExportError(f"STEP transfer failed: {path}")
    if writer.Write(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise StepExportError(f"STEP write failed: {path}")
    return path


def import_step(path: str | Path) -> bd.Shape[Any]:
    """Load a STEP file. Raises StepImportError if the file cannot be read or holds no shape."""
    path = Path(path)
    _quiet()
    status = STEPControl_Reader().ReadFile(str(path))
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise StepImportError(f"cannot read STEP file {path}: {status.name}")
    shape = bd.import_step(path)
    if not shape.faces():
        raise StepImportError(f"STEP file {path} contains no geometry")
    return shape


def read_schema(path: str | Path) -> str:
    """The FILE_SCHEMA entry of a STEP file header, e.g. 'AP242_MANAGED_MODEL_BASED_3D_...'."""
    header = Path(path).read_text(errors="replace")[:4000]
    match = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'", header)
    if not match:
        raise ValueError(f"no FILE_SCHEMA in {path}")
    return match.group(1)
