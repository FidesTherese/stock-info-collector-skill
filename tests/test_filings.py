"""Tests for filing discovery, HTML and PDF statement extraction, identity checks, and local paths."""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_info.filings import (  # noqa: E402
    classify_report_title, convert_pdf_text, discover_filings, read_filing, read_pdf_blocks,
)
from stock_info.providers import DateRange, FilingRef, Symbol  # noqa: E402
from test_providers import FakeSession, html_reply, make_client  # noqa: E402

def build_text_pdf(pages: list[list[tuple[float, float, str]]]) -> bytes:
    """Build a PDF whose pages place text cells at given positions and return the PDF bytes.

    Each distinct non-ASCII character receives a one-byte code from 0x80 upward, and
    a ToUnicode CMap maps every code back to its Unicode character, so pypdf can
    extract Chinese text without embedded font programs. At most 128 distinct
    non-ASCII characters are supported.

    :param pages: list[list[tuple[float, float, str]]]; per page, (x, y, text) cells in points.
    return pdf: bytes; complete PDF file.
    """
    chars = sorted({ch for page in pages for _x, _y, text in page for ch in text if ord(ch) > 126})
    if len(chars) > 128:
        raise ValueError("too many distinct characters")
    codes = {ch: 0x80 + i for i, ch in enumerate(chars)}

    def encode(text: str) -> str:
        """Encode text as a PDF hex string using the private one-byte codes.

        :param text: str; cell text.
        return hex_string: str; `<...>` PDF string.
        """
        return "<" + "".join(f"{codes.get(ch, ord(ch)):02X}" for ch in text) + ">"

    mapping = [f"<{code:02X}> <{ord(ch):04X}>" for ch, code in codes.items()]
    mapping += [f"<{c:02X}> <{c:04X}>" for c in range(32, 127)]
    cmap_lines = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
                  "/CMapName /Fixture def", "1 begincodespacerange", "<00> <FF>", "endcodespacerange"]
    for start in range(0, len(mapping), 100):
        chunk = mapping[start:start + 100]
        cmap_lines += [f"{len(chunk)} beginbfchar", *chunk, "endbfchar"]
    cmap_lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    cmap = "\n".join(cmap_lines).encode("ascii")
    objects: list[bytes] = []
    widths = " ".join("500" if c < 128 else "1000" for c in range(32, 256))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    page_ids = [5 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(f"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /FirstChar 32 /LastChar 255"
                   f" /Widths [{widths}] /ToUnicode 4 0 R >>".encode())
    objects.append(b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream")
    for page in pages:
        ops = [f"BT /F1 10 Tf 1 0 0 1 {x} {y} Tm {encode(text)} Tj ET" for x, y, text in page]
        stream = "\n".join(ops).encode("ascii")
        page_id = 5 + len(objects) - 4
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 595] /Resources << /Font << /F1 3 0 R >>"
                       f" >> /Contents {page_id + 1} 0 R >>".encode())
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


SZ = Symbol("SZ:300059", "SZ", "300059")
SH = Symbol("SH:600519", "SH", "600519")
DETAIL_URL = "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=300059&id=9873388"
INDEX_ANNUAL = """<html><head><meta http-equiv="Content-Type" content="text/html; charset=gb2312"></head><body>
<div class="datelist"><ul>
2026-03-28&nbsp;<a href="/corp/view/vCB_AllBulletinDetail.php?stockid=600519&id=111">贵州茅台2025年年度报告</a><br>
2026-03-28&nbsp;<a href="/corp/view/vCB_AllBulletinDetail.php?stockid=600519&id=112">贵州茅台2025年年度报告摘要</a><br>
2025-03-30&nbsp;<a href="/corp/view/vCB_AllBulletinDetail.php?stockid=600519&id=100">贵州茅台2024年年度报告</a><br>
2019-03-30&nbsp;<a href="/corp/view/vCB_AllBulletinDetail.php?stockid=600519&id=50">贵州茅台2018年年度报告</a><br>
</ul><a href="/corp/go.php/vCB_Bulletin/stockid/600519/page_type/ndbg.phtml?p=2">下一页</a></div></body></html>"""
INDEX_ANNUAL_2 = """<html><head><meta charset="gbk"></head><body><ul>
2024-03-30&nbsp;<a href="/corp/view/vCB_AllBulletinDetail.php?stockid=600519&id=90">贵州茅台2023年年度报告</a><br>
2024-03-30&nbsp;<a href="/corp/view/vCB_AllBulletinDetail.php?stockid=600000&id=91">浦发银行2023年年度报告</a><br>
</ul></body></html>"""
INDEX_HALF = """<html><head><meta charset="gbk"></head><body><ul>
2025-08-20&nbsp;<a href="/corp/view/vCB_AllBulletinDetail.php?stockid=600519&id=80">贵州茅台2025年半年度报告</a><br>
2025-08-20&nbsp;<a href="/corp/view/vCB_AllBulletinDetail.php?stockid=600519&id=81">
贵州茅台2025年半年度报告（英文版）</a><br>
</ul></body></html>"""
DETAIL_HTML = """<html><head><title>东方财富:2025年年度报告</title></head><body>
<p>证券代码：300059 证券简称：东方财富</p>
<p>合并资产负债表</p><p>单位：元 币种：人民币</p>
<table><tr><td>项目</td><td>2025年12月31日</td></tr><tr><td>货币资金</td><td>5.00</td></tr></table>
<p>母公司资产负债表</p><p>编制单位：东方财富 单位：元 币种：人民币</p>
<table><tr><td>项目</td><td>附注</td><td>2025年12月31日</td><td>2024年12月31日</td></tr>
<tr><td>货币资金</td><td></td><td>1,234.00</td><td>1,000.00</td></tr>
<tr><td>母公司所有者权益合计</td><td></td><td>(10.00)</td><td>--</td></tr></table>
<p>母公司利润表</p><p>单位：元 币种：人民币</p>
<table><tr><td>项目</td><td>2025年度</td><td>2024年度</td></tr><tr><td>营业收入</td><td>100</td><td>90</td></tr></table>
<p>母公司现金流量表</p><p>单位：元 币种：人民币</p>
<table><tr><td>项目</td><td>本期发生额</td><td>上期发生额</td></tr><tr><td>经营活动现金流量净额</td><td>7</td><td>6</td></tr></table>
<p><a href="http://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/2026-03-14/123.PDF">下载公告</a></p>
</body></html>"""
PDF_PAGE = [(40, 550, "股票代码：600519"), (40, 520, "母公司资产负债表"), (40, 505, "单位：元 币种：人民币"),
            (40, 480, "项目"), (300, 480, "2025年12月31日"), (500, 480, "2024年12月31日"),
            (40, 465, "货币资金"), (300, 465, "1,234.50"), (500, 465, "1,000.00"),
            (40, 450, "资产总计"), (300, 450, "9,876.00"), (500, 450, "--"), (40, 420, "公司负责人：张三")]


def reference(uri: str = DETAIL_URL, symbol: Symbol = SZ, report_date: date | None = date(2025, 12, 31)) -> FilingRef:
    """Build a filing reference and return it.

    :param uri: str; document URI.
    :param symbol: Symbol; requested symbol.
    :param report_date: date | None; candidate period.
    return reference: FilingRef; reference.
    """
    return FilingRef(symbol=symbol, uri=uri, report_date=report_date, publication_date=date(2026, 3, 14),
                     origin="automatic")


class DiscoveryTests(unittest.TestCase):
    """Check index navigation and candidate selection."""

    def test_titles_are_classified_conservatively(self) -> None:
        """Full reports are accepted; summaries and English versions are skipped."""
        self.assertEqual(classify_report_title("贵州茅台2025年年度报告"), ("ndbg", date(2025, 12, 31)))
        self.assertEqual(classify_report_title("2025年半年度报告"), ("zqbg", date(2025, 6, 30)))
        self.assertEqual(classify_report_title("2025年第一季度报告（更正后）"), ("yjdbg", date(2025, 3, 31)))
        self.assertEqual(classify_report_title("2025年第三季度报告"), ("sjdbg", date(2025, 9, 30)))
        self.assertEqual(classify_report_title("2025年年度报告摘要"), ("skipped", None))
        self.assertEqual(classify_report_title("关于召开股东大会的通知"), (None, None))

    def test_gbk_index_pages_with_pagination(self) -> None:
        """Candidates come from GBK pages, follow same-index pagination, and keep adjacent dates."""
        session = FakeSession([
            (lambda u: u.endswith("ndbg.phtml"), html_reply(INDEX_ANNUAL, "gb2312")),
            (lambda u: "ndbg.phtml?p=2" in u, html_reply(INDEX_ANNUAL_2, "gbk")),
            (lambda u: "zqbg" in u, html_reply(INDEX_HALF, "gbk")),
            (lambda u: "yjdbg" in u, html_reply("<html><body><ul></ul></body></html>")),
            (lambda u: "sjdbg" in u, lambda _u: (500, b"", {})),
        ])
        result = discover_filings(make_client(session), SH, DateRange(date(2023, 1, 1), date(2025, 12, 31)))
        self.assertEqual(result.state, "partial")
        self.assertEqual([(c.report_date.isoformat(), c.uri.rsplit("=", 1)[1]) for c in result.candidates],
                         [("2025-12-31", "111"), ("2025-06-30", "80"), ("2024-12-31", "100"), ("2023-12-31", "90")])
        self.assertEqual(result.candidates[0].publication_date, date(2026, 3, 28))
        self.assertTrue(result.candidates[0].uri.startswith("https://vip.stock.finance.sina.com.cn/"))
        self.assertEqual(result.metadata["skipped_titles"], 2)
        self.assertFalse(result.metadata["coverage_verified"])
        self.assertTrue(result.metadata["pending_urls"])


class HtmlFilingTests(unittest.TestCase):
    """Check HTML statement extraction."""

    def read(self, html: str, ref: FilingRef | None = None):
        """Serve one HTML document and read it.

        :param html: str; document HTML.
        :param ref: FilingRef | None; reference, default the Shenzhen detail page.
        return result: FilingResult; extraction outcome.
        """
        session = FakeSession([(lambda u: True, html_reply(html))])
        return read_filing(make_client(session), ref or reference())

    def test_three_parent_tables_and_attachment(self) -> None:
        """Parent tables are separated from consolidated ones; the PDF attachment is upgraded to HTTPS."""
        result = self.read(DETAIL_HTML)
        self.assertEqual(result.status, "parsed")
        found = sorted((s.scope, s.kind, s.report_date.isoformat(), s.period_basis) for s in result.statements)
        self.assertEqual(found, [
            ("consolidated", "balance", "2025-12-31", "instant"),
            ("parent", "balance", "2024-12-31", "instant"), ("parent", "balance", "2025-12-31", "instant"),
            ("parent", "cash_flow", "2025-12-31", "year_to_date"),
            ("parent", "income", "2024-12-31", "annual"), ("parent", "income", "2025-12-31", "annual")])
        balance = next(s for s in result.statements if s.scope == "parent" and s.kind == "balance"
                       and s.report_date == date(2025, 12, 31))
        self.assertEqual((balance.unit, balance.currency), ("元", "人民币"))
        self.assertEqual([i.value_decimal for i in balance.items], ["1234.00", "-10.00"])
        self.assertEqual(balance.items[0].source_metadata["cells"], ["货币资金", "", "1,234.00", "1,000.00"])
        self.assertIn("column_date_from_title", [i.code for i in result.issues])
        attachments = result.metadata["attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertTrue(attachments[0]["uri"].startswith("https://file.finance.sina.com.cn/"))

    def test_identity_mismatch_and_geometry(self) -> None:
        """A different labelled code is rejected; spanned cells make a table unparsed."""
        mismatch = self.read(DETAIL_HTML.replace("证券代码：300059", "证券代码：600000"))
        self.assertEqual((mismatch.status, mismatch.issues[0].code), ("unparsed", "filing_identity_mismatch"))
        self.assertEqual(mismatch.statements, ())
        spanned = self.read(DETAIL_HTML.replace("<td>附注</td>", '<td colspan="2">附注</td>'))
        self.assertEqual(spanned.status, "partial")
        self.assertIn("ambiguous_statement_table", [i.code for i in spanned.issues])
        self.assertFalse(any(s.scope == "parent" and s.kind == "balance" for s in spanned.statements))

    def test_heading_applies_only_within_window(self) -> None:
        """A heading followed by more than 1,000 characters does not label the next table."""
        far = DETAIL_HTML.replace("<p>母公司利润表</p>", "<p>母公司利润表</p><p>" + "说明" * 600 + "</p>")
        result = self.read(far)
        self.assertFalse(any(s.kind == "income" for s in result.statements))


class PdfFilingTests(unittest.TestCase):
    """Check real pypdf extraction and its failure modes."""

    def test_text_pdf_yields_parent_statement_from_local_file(self) -> None:
        """A ToUnicode PDF under the root produces two dated parent balance sheets."""
        with TemporaryDirectory() as tmp:
            Path(tmp, "doc.pdf").write_bytes(build_text_pdf([PDF_PAGE]))
            result = read_filing(make_client(FakeSession()), reference("doc.pdf", SH, None), root=Path(tmp))
        self.assertEqual((result.status, result.format), ("parsed", "pdf"))
        self.assertEqual(len(result.requests), 0)
        latest = next(s for s in result.statements if s.report_date == date(2025, 12, 31))
        self.assertEqual((latest.scope, latest.kind, latest.unit), ("parent", "balance", "元"))
        self.assertEqual([(i.item_title, i.value_decimal) for i in latest.items],
                         [("货币资金", "1234.50"), ("资产总计", "9876.00")])
        self.assertTrue(latest.locators[0].startswith("page:1:line:"))

    def test_local_path_boundaries(self) -> None:
        """Escapes, UNC paths, and URI authorities are rejected before reading."""
        with TemporaryDirectory() as tmp:
            for uri in ("../outside.pdf", "\\\\server\\share\\x.pdf", "file://host/x.pdf"):
                result = read_filing(make_client(FakeSession()), reference(uri, SH), root=Path(tmp))
                self.assertEqual((result.status, result.issues[0].code), ("fetch_failed", "invalid_path"), uri)

    def test_encrypted_scanned_and_broken_pdfs(self) -> None:
        """Encrypted, image-only, and malformed PDFs are reported explicitly."""
        from io import BytesIO

        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(200, 200)
        blank = BytesIO()
        writer.write(blank)
        writer.encrypt("secret")
        locked = BytesIO()
        writer.write(locked)
        self.assertEqual(read_pdf_blocks(locked.getvalue())[2].code, "encrypted_pdf")
        self.assertEqual(read_pdf_blocks(blank.getvalue())[2].code, "scanned_or_unreadable_pdf")
        self.assertIn(read_pdf_blocks(b"%PDF-1.4\nnot a pdf")[2].code,
                      ("pdf_parse_error", "scanned_or_unreadable_pdf"))

    def test_page_continuation_requires_repeated_headers(self) -> None:
        """Rows at the top of the next page without a repeated header make the table non-simple."""
        page_one = "母公司资产负债表\n项目    2025年12月31日\n货币资金    1.00\n"
        unlabelled = convert_pdf_text([page_one, "存货    2.00\n"])
        self.assertFalse(next(b for b in unlabelled if b.kind == "table").simple)
        repeated = convert_pdf_text([page_one, "母公司资产负债表（续）\n项目    2025年12月31日\n存货    2.00\n"])
        table = next(b for b in repeated if b.kind == "table")
        self.assertTrue(table.simple)
        self.assertEqual([r[0] for r in table.rows], ["项目", "货币资金", "存货"])


if __name__ == "__main__":
    unittest.main()
