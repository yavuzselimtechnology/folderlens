import datetime
import io
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from agentarm.helpers import run_helper
from agentarm.legacy_excel_runtime import ROOT as XLS_RUNTIME_ROOT
from agentarm.workbook_tools import list_sheets
from agentarm.workspace import AgentPolicy, Workspace, digest


def legacy_biff2():
    """Synthetic BIFF2 records (BOF, CODEPAGE, DIMENSIONS, LABEL, NUMBER, BOOLERR, FORMULA, EOF)."""

    def record(kind, data):
        return struct.pack("<HH", kind, len(data)) + data

    text = "Türkçe".encode("cp1254")
    attributes = bytes(3)
    return b"".join(
        (
            record(0x0009, struct.pack("<HH", 2, 0x10)),
            record(0x0042, struct.pack("<H", 1254)),
            record(0x0000, struct.pack("<HHHH", 0, 2, 0, 4)),
            record(0x0004, struct.pack("<HH3sB", 0, 0, attributes, len(text)) + text),
            record(0x0003, struct.pack("<HH3sd", 0, 1, attributes, 12.5)),
            record(0x0005, struct.pack("<HH3sBB", 0, 2, attributes, 1, 0)),
            record(0x0005, struct.pack("<HH3sBB", 1, 0, attributes, 7, 1)),
            record(
                0x0006,
                struct.pack("<HH3sdB", 1, 1, attributes, 3.0, 0)
                + bytes(4)
                + b"\x03\x1e\x03\x00",
            ),
            record(0x000A, b""),
        )
    )


class WorkbookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(AgentPolicy(self.root, allow_write=True))

    def test_legacy_real_binary_read_types_cached_formula_and_sheet_discovery(self):
        data = legacy_biff2()
        (self.root / "old.xls").write_bytes(data)
        readonly = Workspace(AgentPolicy(self.root))
        sheets = list_sheets(readonly, "old.xls")
        self.assertEqual(sheets["sheets"], [{"name": "Sheet 1", "index": 0}])
        result = readonly.read_workbook("old.xls", 0, "A1:D3")
        self.assertEqual(result["sha256"], digest(data))
        self.assertEqual(result["rows"][0][0]["value"], "Türkçe")
        self.assertEqual(result["rows"][0][1]["value"], 12.5)
        self.assertIs(result["rows"][0][2]["value"], True)
        self.assertEqual(result["rows"][1][0]["value"], "#DIV/0!")
        self.assertEqual(result["rows"][1][1]["value"], 3.0)
        self.assertIsNone(result["rows"][2][3]["value"])
        self.assertEqual(result["formula_representation"], "cached_values_only")
        self.assertFalse(readonly.policy.allow_exec)
        with self.assertRaises(ValueError):
            readonly.read_workbook("old.xls", 0, "IW1:IW1")

    def test_sheet_pages_indices_unicode_dates_and_formulas(self):
        book = Workbook()
        book.active.title = "0"
        book.create_sheet("İklim")
        book["İklim"]["A1"] = datetime.datetime(2026, 10, 30, 12, 15)  # noqa: DTZ001 - Excel stores timezone-naive dates.
        book["İklim"]["B1"] = "=1+2"
        book.save(self.root / "book.xlsx")
        first = list_sheets(self.workspace, "book.xlsx", limit=1)
        self.assertEqual(first["sheets"], [{"name": "0", "index": 0}])
        second = list_sheets(
            self.workspace, "book.xlsx", offset=first["next_offset"], limit=1
        )
        self.assertEqual(second["sheets"], [{"name": "İklim", "index": 1}])
        self.assertIsNone(second["next_offset"])
        result = self.workspace.read_workbook("book.xlsx", "1", "A1:B1")
        self.assertEqual(result["sheet"], "İklim")
        self.assertEqual(result["rows"][0][0]["value"], "2026-10-30T12:15:00")
        self.assertEqual(result["rows"][0][1]["value"], "=1+2")
        self.assertEqual(
            self.workspace.read_workbook("book.xlsx", "0", "A1:A1")["sheet"], "0"
        )
        with self.assertRaisesRegex(ValueError, "changed"):
            list_sheets(self.workspace, "book.xlsx", expected_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.workspace.read_workbook("book.xlsx", "0", expected_sha256="0" * 64)

    def test_external_links_are_preserved_without_being_fetched(self):
        from openpyxl.packaging.relationship import Relationship
        from openpyxl.workbook.external_link.external import (
            ExternalBook,
            ExternalLink,
            ExternalSheetNames,
        )

        book = Workbook()
        book.active.title = "Data"
        book.active["A1"] = "='[1]Values'!A1"
        link = ExternalLink(
            externalBook=ExternalBook(
                sheetNames=ExternalSheetNames(sheetName=["Values"]), id="rId1"
            )
        )
        link.file_link = Relationship(
            type="externalLinkPath",
            Target="file:///Z:/never-opened.xlsx",
            TargetMode="External",
        )
        book._external_links.append(link)
        book.save(self.root / "linked.xlsx")
        before = (self.root / "linked.xlsx").read_bytes()
        self.workspace.write_workbook(
            "linked.xlsx", "Data", "B1", [[5]], digest(before)
        )
        with (
            zipfile.ZipFile(io.BytesIO(before)) as source,
            zipfile.ZipFile(self.root / "linked.xlsx") as target,
        ):
            for name in source.namelist():
                if name.startswith("xl/externalLinks/"):
                    self.assertEqual(source.read(name), target.read(name))

    def test_xlsm_update_preserves_synthetic_vba_payload_and_undo(self):
        book = Workbook()
        book.active.title = "Data"
        book.active["A1"] = "old"
        original = io.BytesIO()
        book.save(original)
        output = io.BytesIO()
        binary = (
            b"Synthetic VBA bytes: never executed, tested only for byte preservation."
        )
        with (
            zipfile.ZipFile(original) as source,
            zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
        ):
            for member in source.infolist():
                data = source.read(member)
                if member.filename == "[Content_Types].xml":
                    data = data.replace(
                        b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                        b"application/vnd.ms-excel.sheet.macroEnabled.main+xml",
                    )
                    data = data.replace(
                        b"</Types>",
                        b'<Override PartName="/xl/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/></Types>',
                    )
                if member.filename == "xl/_rels/workbook.xml.rels":
                    data = data.replace(
                        b"</Relationships>",
                        b'<Relationship Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" Target="vbaProject.bin" Id="rIdVBA"/></Relationships>',
                    )
                target.writestr(member, data)
            target.writestr("xl/vbaProject.bin", binary)
        before = output.getvalue()
        (self.root / "macro.xlsm").write_bytes(before)
        changed = self.workspace.write_workbook(
            "macro.xlsm", "Data", "A1", [["Türkçe", "=4+5"]], digest(before)
        )
        self.assertTrue(changed["vba_preservation_enabled"])
        with zipfile.ZipFile(self.root / "macro.xlsm") as after:
            self.assertEqual(after.read("xl/vbaProject.bin"), binary)
        self.assertEqual(
            self.workspace.read_workbook("macro.xlsm", "Data", "B1:B1")["rows"][0][0][
                "value"
            ],
            "=4+5",
        )
        self.workspace.undo_change(changed["undo_id"])
        self.assertEqual((self.root / "macro.xlsm").read_bytes(), before)

    def test_detected_feature_loss_rejected_without_changing_source(self):
        book = Workbook()
        book.active.title = "Data"
        original = io.BytesIO()
        book.save(original)
        output = io.BytesIO()
        with (
            zipfile.ZipFile(original) as source,
            zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
        ):
            for member in source.infolist():
                target.writestr(member, source.read(member))
            target.writestr("xl/embeddings/custom.bin", b"must not disappear")
        before = output.getvalue()
        (self.root / "special.xlsx").write_bytes(before)
        with self.assertRaisesRegex(ValueError, "remove workbook features"):
            self.workspace.write_workbook(
                "special.xlsx", "Data", "A1", [["x"]], digest(before)
            )
        self.assertEqual((self.root / "special.xlsx").read_bytes(), before)
        self.assertFalse(self.workspace.undo)

    def test_validation_readonly_conflict_and_output_budget(self):
        created = self.workspace.write_workbook(
            "book.xlsx", "Data", "A1", [["x" * 200]], None
        )
        readonly = Workspace(AgentPolicy(self.root))
        with self.assertRaises(PermissionError):
            readonly.write_workbook("book.xlsx", "Data", "A1", [[1]], created["sha256"])
        with patch("agentarm.workbook_tools.run_helper") as helper:
            with self.assertRaisesRegex(ValueError, "conflict"):
                self.workspace.write_workbook(
                    "book.xlsx", "Data", "A1", [[1]], "0" * 64
                )
            helper.assert_not_called()
        small = Workspace(AgentPolicy(self.root, max_output_chars=100))
        with self.assertRaisesRegex(ValueError, "too large"):
            small.read_workbook("book.xlsx", "Data", "A1:A1")
        for path, rows in (
            ("new.xlsx", []),
            ("new.xlsx", [[float("nan")]]),
        ):
            with self.assertRaises(ValueError):
                self.workspace.write_workbook(path, "Data", "A1", rows, None)
            self.assertFalse((self.root / path).exists())
        with self.assertRaisesRegex(ValueError, "Unknown fixed"):
            run_helper(self.workspace, "user_command.py", {}, 100)

    def test_new_xlsm_is_macro_enabled_container_without_invented_vba(self):
        result = self.workspace.write_workbook(
            "new.xlsm", "Data", "A1", [["Türkçe", "=1+2"]], None
        )
        self.assertTrue(result["vba_preservation_enabled"])
        with zipfile.ZipFile(self.root / "new.xlsm") as archive:
            self.assertIn(
                b"application/vnd.ms-excel.sheet.macroEnabled.main+xml",
                archive.read("[Content_Types].xml"),
            )
            self.assertNotIn("xl/vbaProject.bin", archive.namelist())
        self.assertEqual(
            self.workspace.read_workbook("new.xlsm", 0, "A1:B1")["rows"][0][1]["value"],
            "=1+2",
        )

    @unittest.skipUnless(
        os.name == "nt" and (XLS_RUNTIME_ROOT / "manifest.json").is_file(),
        "Run --setup-xls for the Windows native XLS integration test",
    )
    def test_native_biff8_roundtrip_partial_edit_and_undo(self):
        from agentarm.legacy_excel_runtime import ready

        ready()  # Explicit fixture setup required; never download during a test/tool call.
        created = self.workspace.write_workbook(
            "native.xls",
            "Veri",
            "A1",
            [["Türkçe 中文", 12.5, True, None], ["=B1*2", "keep"]],
            None,
        )
        before = (self.root / "native.xls").read_bytes()
        self.assertTrue(before.startswith(bytes.fromhex("d0cf11e0a1b11ae1")))
        self.assertEqual(created["format"], "BIFF8")
        values = self.workspace.read_workbook("native.xls", "Veri", "A1:D1")["rows"][0]
        self.assertEqual(
            [c["value"] for c in values], ["Türkçe 中文", 12.5, True, None]
        )
        changed = self.workspace.write_workbook(
            "native.xls", "Veri", "A2", [["literal"]], created["sha256"]
        )
        values = self.workspace.read_workbook("native.xls", 0, "A2:B2")["rows"][0]
        self.assertEqual([c["value"] for c in values], ["literal", "keep"])
        self.workspace.undo_change(changed["undo_id"])
        self.assertEqual((self.root / "native.xls").read_bytes(), before)
        with self.assertRaises(ValueError):
            self.workspace.write_workbook("bad.xls", "Data", "IW1", [[1]], None)
        self.assertFalse((self.root / "bad.xls").exists())

    @unittest.skipUnless(
        os.name == "nt" and (XLS_RUNTIME_ROOT / "manifest.json").is_file(),
        "Run --setup-xls for the Windows native XLS preservation test",
    )
    def test_native_biff8_retains_style_formula_and_opaque_ole_node(self):
        from agentarm.legacy_excel_runtime import _environment, _lock, ready

        runtime = ready()
        source = self.root / "Fixture.java"
        source.write_text(
            """
import java.io.*;
import org.apache.poi.hssf.usermodel.*;
import org.apache.poi.poifs.filesystem.*;
import org.apache.poi.ss.util.CellRangeAddress;
public class Fixture {
 public static void main(String[] args) throws Exception {
  if (args[0].equals("create")) {
   byte[] bytes;
   try (HSSFWorkbook book = new HSSFWorkbook(); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
    var sheet = book.createSheet("Data");
    var row = sheet.createRow(0);
    row.createCell(0).setCellValue("old");
    var style = book.createCellStyle(); style.setDataFormat((short) 4);
    row.getCell(0).setCellStyle(style);
    row.createCell(1).setCellFormula("2+3");
    sheet.addMergedRegion(new CellRangeAddress(2, 2, 0, 1));
    book.write(out); bytes = out.toByteArray();
   }
   try (POIFSFileSystem fs = new POIFSFileSystem(new ByteArrayInputStream(bytes));
        FileOutputStream out = new FileOutputStream(args[1])) {
    fs.getRoot().createDocument("OpaqueFixture", new ByteArrayInputStream(new byte[] {1,2,3,4}));
    fs.writeFilesystem(out);
   }
  } else {
   try (POIFSFileSystem fs = new POIFSFileSystem(new File(args[1]), true);
        HSSFWorkbook book = new HSSFWorkbook(fs)) {
    var row = book.getSheet("Data").getRow(0);
    if (!row.getCell(0).getStringCellValue().equals("new") || row.getCell(0).getCellStyle().getDataFormat() != 4
        || !row.getCell(1).getCellFormula().equals("2+3")) throw new Exception("Workbook content changed");
    try (DocumentInputStream in = fs.createDocumentInputStream("OpaqueFixture")) {
     if (!java.util.Arrays.equals(in.readAllBytes(), new byte[] {1,2,3,4})) throw new Exception("OLE node changed");
    }
   }
  }
 }
}
""",
            encoding="utf-8",
        )
        compiler = next(item for item in _lock()["jars"] if item["compiler"])
        subprocess.run(
            [
                runtime["java"],
                "-Xmx128m",
                "-jar",
                str(XLS_RUNTIME_ROOT / "lib" / compiler["name"]),
                "-17",
                "-classpath",
                runtime["classpath"],
                "-d",
                str(self.root),
                str(source),
            ],
            check=True,
            capture_output=True,
            timeout=30,
            env=_environment(),
        )
        command = [
            runtime["java"],
            "-Xmx128m",
            "-cp",
            str(self.root) + os.pathsep + runtime["classpath"],
            "Fixture",
        ]
        target = self.root / "fixture.xls"
        subprocess.run(
            [*command, "create", str(target)],
            check=True,
            capture_output=True,
            timeout=30,
            env=_environment(),
        )
        self.workspace.write_workbook(
            "fixture.xls", "Data", "A1", [["new"]], digest(target.read_bytes())
        )
        subprocess.run(
            [*command, "inspect", str(target)],
            check=True,
            capture_output=True,
            timeout=30,
            env=_environment(),
        )
        before = target.read_bytes()
        with self.assertRaisesRegex(ValueError, "Native XLS writer failed"):
            self.workspace.write_workbook(
                "fixture.xls", "Data", "B3", [["bad"]], digest(before)
            )
        self.assertEqual(target.read_bytes(), before)

    @unittest.skipUnless(
        os.name in {"nt", "posix"}, "helper memory limit supports Windows/Unix"
    )
    def test_parser_memory_budget_is_enforced_in_an_owned_synthetic_process(self):
        program = (
            "from agentarm.workbook_worker import limit_memory, MEMORY_BYTES; "
            "guard=limit_memory(); payload=bytearray(MEMORY_BYTES*2)"
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"MemoryError", result.stderr)


if __name__ == "__main__":
    unittest.main()
